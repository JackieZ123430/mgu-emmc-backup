# MGU eMMC Backup

A read-only SSH backup tool for the eMMC in a Linux-based BMW MGU. It includes a Windows desktop app and a Python command-line interface. The desktop app currently uses Simplified Chinese.

## Download and run on Windows

1. Download the Windows ZIP from [Releases](../../releases/latest). Extract it and run `MGU-eMMC-Backup.exe`.
2. Enter the MGU SSH address and port. The defaults are user `root`, port `22`, and device `mmcblk0`. Choose password, a private key file, or your SSH agent/default key.
3. Verify the MGU's SSH host fingerprint through a trusted channel. Add the verified key to your OpenSSH `known_hosts`, or select a separate trusted `known_hosts` file in the app.
4. Click **探测 MGU** (Probe MGU). Check the reported device, partition count, and capacities.
5. Choose a destination with enough space for the full device and click **开始备份** (Start backup). The app creates a new subfolder and verifies the images after capture.

To monitor an existing backup, select its directory. To resume an interrupted capture, select the same directory and click **续传备份** (Resume backup). Resume checks the eMMC identity, partition layout, boot session, and saved chunk hashes. If the MGU rebooted, start a new backup in a new directory. Use **校验镜像** (Verify images) to check a completed backup offline.

## Run from source

Install Python 3.10 or later. Install Tkinter for the GUI if your Python distribution does not include it.

```powershell
python -m pip install -r requirements.txt
python mgu_emmc_backup_gui.py
```

CLI examples for Windows PowerShell. Replace `192.0.2.10` with your MGU's address. Add `--password` for an interactive password prompt or `--key PATH` to select a private key. The same SSH host-key verification applies.

```powershell
python mgu_emmc_backup.py probe 192.0.2.10 --disk mmcblk0
python mgu_emmc_backup.py capture 192.0.2.10 "D:\MGU-Backups\P18-raw"
python mgu_emmc_backup.py capture 192.0.2.10 "D:\MGU-Backups\P18-raw" --resume
python mgu_emmc_backup.py verify "D:\MGU-Backups\P18-raw"
```

The CLI capture destination must be a new directory for the first run. The source MGU needs root SSH access, Linux block device information in sysfs, and `dd`. The `mmc` utility is optional and records EXT_CSD when available. Use NTFS or another filesystem that supports files over 4 GiB.

## Backup contents

| File | Contents |
| --- | --- |
| `mmcblk0.img` | Raw eMMC user area, including the partition table and the partitions inside it. |
| `mmcblk0boot0.img`, `mmcblk0boot1.img` | Separate eMMC hardware boot areas. Exposed general-purpose eMMC areas are also captured if present. |
| `manifest.json` | Region sizes, partition offsets, SHA-256 hashes, and capture state. |
| `emmc-extcsd.txt` | eMMC configuration when the device supports reading it with `mmc extcsd read`. |

Use a backup only after capture completes and offline verification passes. Keep the images private. They can contain device-specific data, settings, and credentials.

## Recovery scope

The tool reads the MGU during normal operation. Writes to mounted filesystems can make a live image internally inconsistent. RPMB and storage outside the selected eMMC are outside the capture scope.

For hardware restoration, use an eMMC programmer that can independently access the user area and both boot areas. Confirm the programmer's area numbering, the target capacity, and the original boot configuration before writing. Keep a readback of the target and verify programmed regions before testing the boot. The backup tool has no disk-writing control.

The SSH probe and boot-area reads were tested on one P18 bench unit. Full hardware restoration and boot after restoration have yet to be validated.

## Build the Windows executable

On Windows, use 64-bit Python 3.14 with Tkinter. These were the dependencies used for the first release.

```powershell
python -m pip install -r requirements-build.txt
python -m PyInstaller --onefile --windowed --name MGU-eMMC-Backup mgu_emmc_backup_gui.py
```

The executable appears in `dist`. To rebuild with a modified Paramiko, install the modified library in the build environment before running PyInstaller. The release archive includes dependency notices and the Paramiko source distribution used for the build.

## License

The project source is available under the [MIT License](LICENSE). Dependencies retain their own licenses.
