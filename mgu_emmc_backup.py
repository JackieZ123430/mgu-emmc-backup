"""Read a Linux MGU's eMMC over SSH and verify the resulting raw images."""
import argparse
import getpass
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import sys
from datetime import datetime, timezone

MIB = 1024 * 1024
CHUNK = 128 * MIB
SCHEMA = 1


class BackupPaused(Exception):
    """The caller requested a stop at the last committed chunk."""


def check_stop(stop):
    if stop is not None and stop.is_set():
        raise BackupPaused()


def now():
    return datetime.now(timezone.utc).isoformat()


def channel(client, command):
    _, out, err = client.exec_command(command, timeout=90, get_pty=False)
    return out, err


def query(client, command):
    out, err = channel(client, command)
    with out, err:
        data = out.read(256 * 1024 + 1)
        if len(data) > 256 * 1024:
            raise RuntimeError("Oversized inventory response")
        if out.channel.recv_exit_status():
            raise RuntimeError(err.read(4096).decode(errors="replace").strip())
    return data.decode("utf-8").strip()


def probe(client, disk):
    if not re.fullmatch(r"mmcblk[0-9]+", disk):
        raise ValueError("Use the whole MMC device name, for example mmcblk0")
    script = r'''
set -e
test "$(id -u)" = 0
p=/sys/class/block/@DISK@
test "$(cat "$p/device/type")" = MMC
test "$(cat "$p/removable")" = 0
test -r "$p/device/cid"
printf 'identity %s %s %s\n' "$(cat "$p/device/cid")" "$(cat /proc/sys/kernel/random/boot_id)" "$(cat "$p/queue/logical_block_size")"
for node in /sys/class/block/@DISK@*; do
    [ -r "$node/size" ] || continue
    name=${node##*/}
    [ -b "/dev/$name" ] || exit 1
    if [ -r "$node/partition" ]; then
        printf 'partition %s %s %s\n' "$name" "$(cat "$node/start")" "$(cat "$node/size")"
    else
        printf 'region %s %s\n' "$name" "$(cat "$node/size")"
    fi
done
for node in /dev/mmc*rpmb*; do
    [ -e "$node" ] || continue
    printf 'rpmb %s\n' "${node##*/}"
done
'''.replace("@DISK@", disk)
    lines = query(client, script).splitlines()
    tag, cid, boot, sector = lines[0].split()
    if tag != "identity" or not re.fullmatch(r"[0-9a-fA-F]{32}", cid):
        raise RuntimeError("Could not establish MMC identity")
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", boot):
        raise RuntimeError("Could not establish kernel boot identity")
    regions, partitions, rpmb = {}, {}, []
    for line in lines[1:]:
        kind, name, *fields = line.split()
        if kind == "rpmb":
            rpmb.append(name)
            continue
        if not re.fullmatch(disk + r"(?:boot[01]|gp[0-9]+|p[0-9]+)?", name):
            raise RuntimeError(f"Unexpected MMC node {name}")
        values = [int(x) * 512 for x in fields]  # sysfs start/size use 512-byte units
        if kind == "region" and len(values) == 1 and values[0] > 0:
            regions[name] = values[0]
        elif kind == "partition" and len(values) == 2 and values[1] > 0:
            partitions[name] = {"offset": values[0], "bytes": values[1]}
        else:
            raise RuntimeError(f"Unexpected MMC inventory row for {name}")
    required = {disk, disk + "boot0", disk + "boot1"}
    if not required <= regions.keys() or not partitions:
        raise RuntimeError("Expected a user area, both boot areas and a partition table")
    if any(p["offset"] < 0 or p["offset"] + p["bytes"] > regions[disk]
           for p in partitions.values()):
        raise RuntimeError("Partition layout exceeds the MMC user area")
    return {
        "disk": disk, "cid_sha256": hashlib.sha256(cid.lower().encode()).hexdigest(),
        "boot_id": boot, "logical_sector_bytes": int(sector), "regions": regions,
        "partitions": partitions, "observed_rpmb_nodes": sorted(rpmb),
    }


def save_manifest(path, state):
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as stream:
        json.dump(state, stream, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    tmp.replace(path)


def read_region(client, name, offset, length, destination, whole_hash):
    # A large block on the aligned range and a sector block for the final tail.
    block = MIB if offset % MIB == 0 and length % MIB == 0 else 512
    command = (f"dd if={shlex.quote('/dev/' + name)} bs={block} "
               f"skip={offset // block} count={length // block}")
    out, err = channel(client, command)
    chunk_hash = hashlib.sha256()
    with out, err:
        try:
            received = 0
            while received < length:
                data = out.read(min(MIB, length - received))
                if not data:
                    break
                destination.write(data)
                chunk_hash.update(data)
                whole_hash.update(data)
                received += len(data)
            extra = out.read(1)
            status = out.channel.recv_exit_status() if not extra else -1
            if received != length or extra or status:
                detail = err.read(4096).decode(errors="replace").strip() if not extra else ""
                raise RuntimeError(f"{name} at {offset}: got {received}/{length}, exit {status}. {detail}")
        finally:
            out.channel.close()
    return {"bytes": length, "sha256": chunk_hash.hexdigest()}


def check_prefix(file, chunks, stop=None):
    whole = hashlib.sha256()
    for chunk in chunks:
        check_stop(stop)
        remaining = chunk["bytes"]
        if not 0 < remaining <= CHUNK:
            raise RuntimeError("Invalid chunk length in manifest")
        digest = hashlib.sha256()
        while remaining:
            data = file.read(min(remaining, MIB))
            if not data:
                raise RuntimeError("Image shorter than recorded backup")
            remaining -= len(data)
            whole.update(data)
            digest.update(data)
        if digest.hexdigest() != chunk["sha256"]:
            raise RuntimeError(f"Checksum mismatch in {file.name}")
    return whole


def capture(client, disk, folder, resume=False, *, report=print, stop=None):
    inventory = probe(client, disk)
    folder = Path(folder)
    manifest = folder / "manifest.json"
    if not resume:
        folder.mkdir(parents=True, exist_ok=False)
    lock = folder / "capture.lock"
    with lock.open("x", encoding="utf-8") as marker:
        marker.write(f"{os.getpid()}\n")
    try:
        if resume:
            state = json.loads(manifest.read_text(encoding="utf-8"))
            if state["schema"] != SCHEMA or state["source"] != inventory:
                raise RuntimeError("Resume requires the same device, layout and boot session. Start in a new folder.")
        else:
            state = {"schema": SCHEMA, "created_utc": now(), "source": inventory,
                     "consistent_snapshot": False, "complete": False, "images": {}}
        regions = inventory["regions"]
        required = sum(size - sum(c["bytes"] for c in state["images"].get(name, {}).get("chunks", []))
                       for name, size in regions.items())
        save_manifest(manifest, state)
        if shutil.disk_usage(folder).free < required + 256 * MIB:
            raise RuntimeError(f"Need {required / (1024**3):.2f} GiB plus 256 MiB free")
        extcsd = folder / "emmc-extcsd.txt"
        if not extcsd.exists():
            try:
                settings = query(client, f"mmc extcsd read /dev/{disk}")
                if "PARTITION_CONFIG" not in settings:
                    raise RuntimeError("PARTITION_CONFIG unavailable")
                extcsd.write_text(settings + "\n", encoding="utf-8")
            except (RuntimeError, OSError) as exc:
                print(f"EXT_CSD not saved: {exc}", file=sys.stderr, flush=True)
        for name in sorted(regions, key=lambda device: (device == disk, device)):
            check_stop(stop)
            size = regions[name]
            image = state["images"].setdefault(name, {"bytes": size, "chunks": []})
            final = folder / f"{name}.img"
            part = folder / f"{name}.img.partial"
            if final.exists():
                verify_image(final, image, size, stop)
                continue
            if not part.exists() and image["chunks"]:
                raise RuntimeError(f"Missing partial image for {name}")
            with part.open("r+b" if part.exists() else "x+b") as output:
                whole = check_prefix(output, image["chunks"], stop)
                offset = output.tell()
                if offset > size:
                    raise RuntimeError(f"Recorded prefix exceeds size of {name}")
                output.truncate(offset)
                while offset < size:
                    check_stop(stop)
                    length = min(CHUNK, size - offset)
                    aligned = length - length % MIB
                    length = aligned or length
                    chunk = read_region(client, name, offset, length, output, whole)
                    output.flush()
                    os.fsync(output.fileno())
                    offset += length
                    image["chunks"].append(chunk)
                    save_manifest(manifest, state)
                    report(f"{name}: {offset / MIB:,.1f}/{size / MIB:,.1f} MiB", flush=True)
                image["sha256"] = whole.hexdigest()
                save_manifest(manifest, state)
            part.replace(final)
        if probe(client, disk) != inventory:
            raise RuntimeError("Source inventory changed during capture")
        state["complete"] = True
        state["completed_utc"] = now()
        save_manifest(manifest, state)
    finally:
        lock.unlink()


def verify_image(path, image, size, stop=None):
    if path.stat().st_size != size or image["bytes"] != size:
        raise RuntimeError(f"Wrong image length for {path.name}")
    with path.open("rb") as file:
        actual = check_prefix(file, image["chunks"], stop).hexdigest()
        if file.tell() != size or actual != image["sha256"]:
            raise RuntimeError(f"Wrong image digest for {path.name}")


def verify(folder, *, report=print, stop=None):
    state = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    if state["schema"] != SCHEMA or not state["complete"]:
        raise RuntimeError("Manifest does not mark this capture complete")
    for name, size in state["source"]["regions"].items():
        if not re.fullmatch(r"mmcblk[0-9]+(?:boot[01]|gp[0-9]+)?", name):
            raise RuntimeError("Invalid image name in manifest")
        image = state["images"][name]
        report(f"Verifying {name}.img", flush=True)
        verify_image(folder / f"{name}.img", image, size, stop)
        report(f"OK  {name}.img  {size} bytes  {image['sha256']}", flush=True)


def connect_ssh(host, *, user="root", port=22, key=None, known_hosts=None,
                password=None, passphrase=None):
    import paramiko

    client = paramiko.SSHClient()
    try:
        client.load_system_host_keys()
        if known_hosts:
            client.load_host_keys(str(known_hosts))
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
        client.connect(
            host, port=port, username=user, key_filename=str(key) if key else None,
            password=password, passphrase=passphrase,
            allow_agent=password is None and not key,
            look_for_keys=password is None and not key,
            timeout=15, banner_timeout=15, auth_timeout=20,
        )
        client.get_transport().set_keepalive(15)
        return client
    except BaseException:
        client.close()
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    for action in ("probe", "capture"):
        connect = commands.add_parser(action)
        connect.add_argument("host")
        connect.add_argument("--user", default="root")
        connect.add_argument("--port", type=int, default=22)
        connect.add_argument("--disk", default="mmcblk0")
        connect.add_argument("--key", type=Path)
        connect.add_argument("--known-hosts", type=Path)
        connect.add_argument("--password", action="store_true")
        if action == "capture":
            connect.add_argument("folder", type=Path)
            connect.add_argument("--resume", action="store_true")
    commands.add_parser("verify").add_argument("folder", type=Path)
    args = parser.parse_args()
    if args.action == "verify":
        verify(args.folder)
        return
    password = getpass.getpass("SSH password: ") if args.password else None
    with connect_ssh(args.host, user=args.user, port=args.port, key=args.key,
                     known_hosts=args.known_hosts, password=password) as client:
        if args.action == "probe":
            print(json.dumps(probe(client, args.disk), indent=2))
        else:
            capture(client, args.disk, args.folder, args.resume)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Interrupted. Completed chunks are recorded in manifest.json.", file=sys.stderr)
        sys.exit(130)
    except Exception as exc:
        print(f"Backup error: {exc}", file=sys.stderr)
        sys.exit(1)
