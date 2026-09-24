# Fedora 44 Image Builder bootc PoC

This builds a small Fedora 44 bootc container with Image Builder's
`bootable_container` pipeline, converts it to qcow2 with Image Builder, and
boots the disk in QEMU. Podman is included in the guest but is not used to
build the container. Skopeo only imports Image Builder's OCI archive into
local container storage for the conversion step.

The single input, [`fedora44-bootc.yaml`](fedora44-bootc.yaml), has the usual
Image Builder `distros` and `image_types` sections. `ibbootc.py` adds:

- `blueprint_config`: blueprint options such as the login user;
- `extensions.files`: local file sources, destinations, modes, and required
  SHA-256 checksums;
- `extensions.scripts`: inline Bash run during image construction.

Image Builder 83 does not ship a Fedora 44 `iot-bootable-container` image
type. The script splits the one-file definition into the YAML tree accepted
by Image Builder's hidden `--force-defs-dir` flag.

## Requirements

Fedora 44 x86_64 with `image-builder` and `osbuild` for the default mode, or
`podman` for the upstream container mode, plus `rpm-build`, `createrepo_c`,
`python3-pyyaml`, `skopeo`, `qemu-img`, and `qemu-system-x86_64`. The user
needs passwordless `sudo` for Image Builder or Podman and for Skopeo. The
container mode runs `ghcr.io/osbuild/image-builder-cli:latest` with Podman's
rootful storage mounted so the conversion step can use the image imported by
Skopeo. The build downloads packages and needs several GiB of free disk space.
The boot test uses QEMU's KVM accelerator, so the user running it needs access
to `/dev/kvm`.

## Run

```sh
python3 ibbootc.py pin              # insert checksums for new file entries
python3 ibbootc.py check            # reject changed or unpinned files
python3 ibbootc.py build-container  # RPM, repo, then bootc OCI archive
python3 ibbootc.py convert          # import archive with skopeo, then qcow2
python3 ibbootc.py boot             # interactive serial QEMU console
```

Add `--image-builder-container` to `build-container`, `convert`, or `build` to
run Image Builder from its upstream container with Podman. For example,
`python3 ibbootc.py build --image-builder-container` uses the container for
both build steps; without the flag the locally installed `image-builder` is
used.

The `boot` command uses KVM acceleration and QEMU's temporary snapshot mode,
so guest changes made during that test do not modify the qcow2 artifact.

## GitHub Actions and GHCR

The `Build bootc image` workflow runs on every push and can also be started
manually from the Actions tab. Before publishing, it boots the generated qcow2
with QEMU's software emulator and waits up to five minutes for the serial login
prompt. It builds through the upstream Image Builder container and publishes
the bootc image to `ghcr.io/<owner>/ibbootc` with a `sha-<commit>` tag. Builds
from `main` also update `latest`.

The qcow2 disk is attached to the matching image as an OCI artifact. Find its
digest with ORAS, then pull it by digest:

```sh
oras discover ghcr.io/<owner>/ibbootc:sha-<commit>
oras pull ghcr.io/<owner>/ibbootc@sha256:<disk-artifact-digest>
```

The artifact is marked as `application/vnd.ibbootc.disk.v1` and contains the
original `.qcow2` file.

For the end-to-end check, log in on the serial console as `poc` (password
`poc`) and run:

```sh
rpm-ostree status
findmnt -T /usr -n -o TARGET,FSTYPE,SOURCE
```

`rpm-ostree status` should show the active deployment, and `findmnt` should
show an `overlay` filesystem with source `composefs`.

`python3 ibbootc.py build` runs both build steps. The example user is `poc`
with password `poc`; change the example password before using the artifact
outside an isolated test. Output and generated intermediate files are under
`work/`. `--definition` and `--work` select other locations.

The runtime package set includes `nss-altfiles` for OSTree account lookups.
OSTree places image users in `/usr/lib/passwd`, and Fedora's NSS policy uses
the `altfiles` source to resolve them; without the module, serial login as
`poc` fails. The image also includes `rpm-ostree` and `composefs`; the OSTree
deployment is mounted with composefs during boot.

The generated RPM owns each extra file. Its `%posttrans` calls one generated
Bash script containing the inline snippets in listed order. The wrapper
puts only this RPM in the final blueprint package transaction; place all
other packages in `image_types.<type>.package_sets.os`. That makes the
scripts run after the other RPM transactions and their scriptlets. Scripts
run in the image build root, without a running systemd or host device access.
The installed runner remains under `/usr/libexec/ibbootc/posttrans.sh` so its
contents can be inspected in the image.

## Packages used during conversion

This PoC uses the same bootc container as the final OS and as Image Builder's
buildroot for qcow2 conversion. The package list in the YAML labels the
conversion requirements separately: `python3` runs OSBuild's host-provided
runner, `dosfstools` and `e2fsprogs` create the disk filesystems, and
`qemu-img` exports qcow2. These packages also appear in the final OS under
the one-container design. GRUB and shim packages let the bootc container
generate BIOS and EFI bootupd metadata before conversion.

The conversion uses `--bootc-default-fs ext4`, as Fedora bootc does not
provide a default root filesystem in this Image Builder release. The
container is installed to qcow2 through Image Builder's bootc/OSTree path.
The pinned `files/prepare-root.conf` supplies the OSTree configuration that
`bootc install` expects. `composefs.enabled = yes` requires composefs for the
booted deployment instead of silently falling back to the classic OSTree
layout.
