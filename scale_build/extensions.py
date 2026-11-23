import errno
import logging
import os
import shutil
from pathlib import Path

import requests

from .image.utils import run_in_chroot
from .utils.kernel import get_kernel_version
from .utils.manifest import get_manifest
from .utils.paths import TMPFS, PKG_DIR
from .utils.run import run
from .driver_materials import validate_nvidia_material, verify_nvidia_download, verify_nvidia_payload
from .build_receipts import checksum, write_checked_json

logger = logging.getLogger(__name__)


def build_extensions(rootfs_image, dst_dir):
    chroot = os.path.join(TMPFS, "extensions_chroot")
    chroot_base = os.path.join(TMPFS, "extensions_chroot_base")

    if os.path.exists(chroot_base):
        shutil.rmtree(chroot_base)
    os.makedirs(chroot_base)
    run(["unsquashfs", "-dest", chroot_base, rootfs_image])

    for klass, name in [(DevToolsExtension, "dev-tools"), (NvidiaExtension, "nvidia")]:
        klass(rootfs_image, chroot_base, chroot).build(name, f"{dst_dir}/{name}.raw")


class Extension:
    def __init__(self, base_image: str, chroot_base: str, chroot: str):
        """
        :param base_image: rootfs squashfs image path
        :param chroot_base: a path where `base_image` is extracted
            (it will be used to compare which files were modified and should be included in the extension image)
        :param chroot: a path which will be used as chroot for extension install
        """
        self.base_image = base_image
        self.chroot_base = chroot_base
        self.chroot = chroot

    def build(self, name, dst_path):
        if os.path.exists(self.chroot):
            shutil.rmtree(self.chroot)
        os.makedirs(self.chroot)

        run(["unsquashfs", "-dest", self.chroot, self.base_image])

        os.makedirs(os.path.join(self.chroot, "proc"), exist_ok=True)
        run(["mount", "proc", os.path.join(self.chroot, "proc"), "-t", "proc"])
        os.makedirs(os.path.join(self.chroot, "sys"), exist_ok=True)
        run(["mount", "sysfs", os.path.join(self.chroot, "sys"), "-t", "sysfs"])
        os.makedirs(os.path.join(self.chroot, "packages"), exist_ok=True)
        run(["mount", "--bind", PKG_DIR, os.path.join(self.chroot, "packages")])
        try:
            shutil.copyfile("/etc/resolv.conf", f"{self.chroot}/etc/resolv.conf")

            self.build_impl()
        finally:
            run(["umount", os.path.join(self.chroot, "packages")])
            run(["umount", os.path.join(self.chroot, "sys")])
            run(["umount", os.path.join(self.chroot, "proc")])

        self.build_extension(name, dst_path)

    def build_impl(self):
        raise NotImplementedError

    def build_extension(self, name, dst_path):
        # %n is relative to the source transfer root; %f includes its sender path.
        changed_files = run(
            ["rsync", "-an", "--out-format=%n", f"{self.chroot}/", f"{self.chroot_base}/"],
            log=False,
        ).stdout.splitlines()
        sysext_files = set()
        for filename in changed_files:
            if os.path.isabs(filename) or ".." in filename.split("/"):
                raise ValueError("Extension diff path escapes its source tree")
            filename = os.path.normpath(filename)
            if filename.startswith("usr/") and filename != "usr/src" and not filename.startswith("usr/src/"):
                sysext_files.add(filename)

        payload = {f for f in sysext_files if os.path.isfile(os.path.join(self.chroot, f))
                   or os.path.islink(os.path.join(self.chroot, f))}
        required = {"dev-tools": {"usr/bin/make", "usr/bin/sshpass"}, "nvidia": {"usr/bin/nvidia-smi"}}
        if not payload or not required.get(name, set()).issubset(payload):
            raise ValueError(f"Extension {name} is missing its expected /usr payload")
        if name == "nvidia" and not any(
            f.startswith("usr/lib/modules/") and os.path.basename(f) in
            ("nvidia.ko", "nvidia.ko.xz", "nvidia.ko.gz", "nvidia.ko.zst") for f in payload
        ):
            raise ValueError("NVIDIA extension is missing its kernel module payload")

        for root, dirs, files in os.walk(self.chroot, topdown=False):
            for f in files:
                path = os.path.relpath(os.path.abspath(os.path.join(root, f)), self.chroot)
                if path not in sysext_files:
                    os.unlink(os.path.join(root, f))

            for d in dirs:
                path = os.path.join(root, d)
                if os.path.islink(path):
                    if os.path.relpath(path, self.chroot) not in sysext_files:
                        os.unlink(path)
                    continue
                try:
                    os.rmdir(path)
                except OSError as e:
                    if e.errno == errno.ENOTEMPTY:
                        pass
                    else:
                        raise

        os.makedirs(f"{self.chroot}/usr/lib/extension-release.d", exist_ok=True)
        with open(f"{self.chroot}/usr/lib/extension-release.d/extension-release.{name}", "w") as f:
            f.write("ID=_any\n")

        self.verify_payload()
        run(["mksquashfs", self.chroot, dst_path, "-comp", "xz"])

    def verify_payload(self):
        pass

    def run(self, cmd: list[str]):
        run_in_chroot(cmd, chroot=self.chroot)


class DevToolsExtension(Extension):
    def build_impl(self):
        # Make `install-dev-tools` think that this is not necessary
        os.unlink(os.path.join(self.chroot, "usr/local/libexec/disable-rootfs-protection"))

        self.run(["install-dev-tools"])


class NvidiaExtension(Extension):
    binaries = ("apt", "apt-config", "dpkg")
    temporary_packages = ["gcc", "make", "pkg-config"]
    permanent_packages = ["libvulkan1", "nvidia-container-toolkit", "vulkan-validationlayers"]

    def build_impl(self):
        kernel_version = get_kernel_version(self.chroot)
        self.kernel_version = kernel_version
        self.material = validate_nvidia_material(get_manifest()['extensions']['nvidia'])

        for binary in self.binaries:
            os.unlink(os.path.join(self.chroot, f"usr/local/bin/{binary}"))
            os.chmod(os.path.join(self.chroot, f"usr/bin/{binary}"), 0o755)

        self.add_nvidia_repository()
        self.run(["apt", "update"])
        self.run(["apt", "-y", "install"] + self.temporary_packages + self.permanent_packages)

        self.install_nvidia_driver(kernel_version)

        self.run(["apt", "-y", "remove"] + self.temporary_packages)
        self.run(["apt", "-y", "autoremove"])

    def verify_payload(self):
        self.qualification = verify_nvidia_payload(
            self.chroot, self.kernel_version, self.material,
            lambda path, field: run(['modinfo', '-F', field, str(path)], log=False).stdout,
        )

    def build(self, name, dst_path):
        super().build(name, dst_path)
        self.qualification['artifact'] = {'filename': Path(dst_path).name,
                                          'sha256': checksum(dst_path), 'size_bytes': Path(dst_path).stat().st_size}
        write_checked_json(str(dst_path) + '.qualification.json', self.qualification)

    def add_nvidia_repository(self):
        r = requests.get("https://nvidia.github.io/libnvidia-container/gpgkey")
        r.raise_for_status()

        with open(f"{self.chroot}/key.gpg", "w") as f:
            f.write(r.text)

        self.run(["gpg", "-o", "/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg", "--dearmor", "/key.gpg"])

        with open(f"{self.chroot}/etc/apt/sources.list.d/nvidia-container-toolkit.list", "w") as f:
            f.write("deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] "
                    "https://nvidia.github.io/libnvidia-container/stable/deb/$(ARCH) /")

    def download_nvidia_driver(self):
        material = validate_nvidia_material(get_manifest()['extensions']['nvidia'])
        version = material['current']
        filename = f"NVIDIA-Linux-x86_64-{version}-no-compat32.run"
        result = f"{self.chroot}/{filename}"

        self.run(["wget", "-c", "-O", f"/{filename}", material['source_url']])
        verify_nvidia_download(result, material)
        self.run(['sh', f'/{filename}', '--check'])

        os.chmod(result, 0o755)
        return result

    def install_nvidia_driver(self, kernel_version):
        driver = self.download_nvidia_driver()

        self.run(
            [
                f"/{os.path.basename(driver)}",
                "--skip-module-load",
                "--silent",
                f"--kernel-name={kernel_version}",
                "--allow-installation-with-running-driver",
                "--no-rebuild-initramfs",
                "--kernel-module-type=open",
            ]
        )

        self.retain_nvidia_vulkan_icd()
        os.unlink(driver)

    def retain_nvidia_vulkan_icd(self):
        source = Path(self.chroot) / 'etc/vulkan/icd.d/nvidia_icd.json'
        destination = Path(self.chroot) / 'usr/share/vulkan/icd.d/nvidia_icd.json'
        if source.is_symlink() or not source.is_file() or not source.stat().st_size:
            raise ValueError('NVIDIA installer did not produce a regular Vulkan ICD descriptor')
        if destination.exists() or destination.is_symlink():
            raise ValueError('NVIDIA Vulkan ICD destination already exists')
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
