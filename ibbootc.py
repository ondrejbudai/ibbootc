#!/usr/bin/env python3
"""Build an rpm-ostree bootc image from an extended Image Builder YAML definition."""

import argparse
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile

import yaml
from yaml.nodes import MappingNode, SequenceNode


PAYLOAD_NAME = "ibbootc-payload"
CONTAINER_REF = "localhost/ibbootc:poc"
POSTTRANS_PATH = "/usr/libexec/ibbootc/posttrans.sh"


class UniqueKeyLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects duplicate keys in a mapping."""


def _construct_unique_mapping(loader, node, deep=False):
    seen = set()
    for key_node, _ in node.value:
        if key_node.tag == "tag:yaml.org,2002:merge":
            # Keep YAML merge semantics; explicit keys may override merged ones.
            continue
        key = loader.construct_object(key_node, deep=deep)
        if key in seen:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate key {key!r}",
                key_node.start_mark,
            )
        seen.add(key)
    return yaml.SafeLoader.construct_mapping(loader, node, deep=deep)


UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping
)


class HashingReader:
    """A file-like reader that hashes exactly the bytes consumed by tarfile."""

    def __init__(self, stream):
        self.stream = stream
        self.digest = hashlib.sha256()

    def read(self, size=-1):
        data = self.stream.read(size)
        self.digest.update(data)
        return data

    def hexdigest(self):
        return self.digest.hexdigest()


def run(args, *, root=False, env=None):
    command = ["sudo", *args] if root and os.geteuid() != 0 else args
    print("+", " ".join(str(arg) for arg in command), flush=True)
    subprocess.run(command, check=True, env=env)


def read_definition(path):
    with path.open(encoding="utf-8") as stream:
        try:
            document = yaml.load(stream, Loader=UniqueKeyLoader)
        except yaml.YAMLError as error:
            raise ValueError(f"invalid definition YAML: {error}") from error
    if not isinstance(document, dict):
        raise ValueError("definition must be a YAML mapping")
    for key in ("distros", "image_types"):
        if key not in document:
            raise ValueError(f"missing {key} in definition")
    if not isinstance(document["distros"], list) or len(document["distros"]) != 1:
        raise ValueError("this PoC supports exactly one distro")
    if not isinstance(document["image_types"], dict) or len(document["image_types"]) != 1:
        raise ValueError("this PoC supports exactly one image type")
    distro = document["distros"][0]
    if not isinstance(distro, dict):
        raise ValueError("the distro definition must be a mapping")
    if not isinstance(distro.get("name"), str) or not re.fullmatch(
        r"[A-Za-z0-9_][A-Za-z0-9_.-]*", distro["name"]
    ):
        raise ValueError("the distro definition needs a safe name")
    if not isinstance(distro.get("defs_path"), str) or not distro["defs_path"]:
        raise ValueError("the distro definition needs a defs_path")
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", distro["defs_path"]):
        raise ValueError("unsafe defs_path")
    image_type_name = next(iter(document["image_types"]))
    if not isinstance(image_type_name, str) or not re.fullmatch(
        r"[A-Za-z0-9_][A-Za-z0-9_.-]*", image_type_name
    ):
        raise ValueError("the image type needs a safe name")
    image_type = document["image_types"][image_type_name]
    if not isinstance(image_type, dict):
        raise ValueError("the image type definition must be a mapping")
    if "package_sets" not in image_type or not isinstance(image_type["package_sets"], dict):
        raise ValueError("the image type needs package_sets as a mapping")
    if PAYLOAD_NAME in _package_names(image_type["package_sets"]):
        raise ValueError(f"{PAYLOAD_NAME} must not be listed in the image definition package sets")
    return document


def _package_names(value):
    """Collect package names from an image definition's package-set structure."""
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "include" and isinstance(child, list):
                for package in child:
                    if isinstance(package, str):
                        yield package
            else:
                yield from _package_names(child)
    elif isinstance(value, list):
        for child in value:
            yield from _package_names(child)


def extension_config(document):
    extensions = document.get("extensions", {})
    if not isinstance(extensions, dict):
        raise ValueError("extensions must be a mapping")
    return extensions


def validate_scripts(document):
    scripts = extension_config(document).get("scripts", [])
    if not isinstance(scripts, list) or not all(isinstance(script, str) for script in scripts):
        raise ValueError("extensions.scripts must be a list of inline strings")
    return scripts


def validate_blueprint_config(document):
    blueprint = document.get("blueprint_config", {})
    if not isinstance(blueprint, dict):
        raise ValueError("blueprint_config must be a mapping")
    if any(key in blueprint for key in ("packages", "groups", "modules", "enabled_modules")):
        raise ValueError("put packages and groups in image_types.package_sets.os; the payload must be the only final transaction package")
    return blueprint


def canonical_destination(destination, source):
    if not isinstance(destination, str) or not destination.startswith("/"):
        raise ValueError(f"destination for {source} must be absolute")
    components = destination[1:].split("/")
    if (
        destination == "/"
        or any(component in ("", ".", "..") for component in components)
        or not re.fullmatch(r"/[A-Za-z0-9_./+@-]+", destination)
    ):
        raise ValueError(f"unsafe destination: {destination}")
    if destination == "/usr/etc" or destination.startswith("/usr/etc/"):
        raise ValueError("use /etc for image configuration; OSTree preparation moves it to /usr/etc")
    return destination


def check_destination_collisions(destinations):
    for index, destination in enumerate(destinations):
        for previous in destinations[:index]:
            if (
                destination == previous
                or destination.startswith(previous + "/")
                or previous.startswith(destination + "/")
            ):
                raise ValueError(f"duplicate or conflicting destinations: {previous} and {destination}")


def reset_directory(path):
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)


def add_tar_file(archive, definition, item, destination):
    source = source_path(definition, item)
    pin = item["sha256"]
    with source.open("rb") as stream:
        info = tarfile.TarInfo("payload/tree" + destination)
        info.size = os.fstat(stream.fileno()).st_size
        info.mode = int(item.get("mode", "0644"), 8)
        info.uid = info.gid = 0
        info.uname = info.gname = "root"
        info.mtime = 0
        info.pax_headers = {}
        hashing_stream = HashingReader(stream)
        archive.addfile(info, hashing_stream)
    actual = hashing_stream.hexdigest()
    if actual != pin:
        raise ValueError(f"checksum mismatch for {source}: expected {pin}, got {actual}")


def source_path(definition, item):
    source = item.get("source")
    if not isinstance(source, str) or not source:
        raise ValueError("each extension file needs a source path")
    path = definition.parent / source
    if not path.is_file():
        raise ValueError(f"source file does not exist: {path}")
    return path


def digest_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_files(definition, document, *, verify_content=True):
    files = extension_config(document).get("files", [])
    if not isinstance(files, list):
        raise ValueError("extensions.files must be a list")
    destinations = []
    for item in files:
        if not isinstance(item, dict):
            raise ValueError("each extension file must be a mapping")
        source = source_path(definition, item)
        destination = canonical_destination(item.get("destination"), source)
        if destination == POSTTRANS_PATH:
            raise ValueError(f"reserved destination: {destination}")
        destinations.append(destination)
        pin = item.get("sha256")
        if not isinstance(pin, str) or not re.fullmatch(r"[0-9a-f]{64}", pin):
            raise ValueError(f"missing or invalid sha256 for {source}; run 'pin' first")
        if verify_content:
            actual = digest_file(source)
            if pin != actual:
                raise ValueError(f"checksum mismatch for {source}: expected {pin}, got {actual}")
        mode = item.get("mode", "0644")
        if not isinstance(mode, str) or not re.fullmatch(r"0?[0-7]{3}", mode):
            raise ValueError(f"invalid mode for {source}: use a quoted octal string")
    check_destination_collisions([*destinations, POSTTRANS_PATH])
    return files


def pin_missing(definition, document):
    """Insert pins at YAML source lines so comments and formatting survive."""
    extension_config(document)
    root = yaml.compose(definition.read_text(encoding="utf-8"))
    if not isinstance(root, MappingNode):
        raise ValueError("definition must be a YAML mapping")

    def lookup(mapping, name):
        return next((value for key, value in mapping.value if key.value == name), None)

    extensions = lookup(root, "extensions")
    files_node = lookup(extensions, "files") if isinstance(extensions, MappingNode) else None
    if files_node is None:
        print("No extension files to pin")
        return
    if not isinstance(files_node, SequenceNode):
        raise ValueError("extensions.files must be a list")
    lines = definition.read_text(encoding="utf-8").splitlines(keepends=True)
    insertions = []
    for item in files_node.value:
        if not isinstance(item, MappingNode):
            raise ValueError("each extension file must be a mapping")
        if lookup(item, "sha256") is not None:
            continue
        source_node = lookup(item, "source")
        if source_node is None or source_node.start_mark.line != source_node.end_mark.line:
            raise ValueError("source must be a single-line scalar for pin")
        source = source_path(definition, {"source": source_node.value})
        key_node = next(key for key, value in item.value if key.value == "source")
        indentation = " " * key_node.start_mark.column
        insertions.append((source_node.end_mark.line + 1, f"{indentation}sha256: {digest_file(source)}\n"))
    for line, value in reversed(insertions):
        lines.insert(line, value)
    if insertions:
        definition.write_text("".join(lines), encoding="utf-8")
    print(f"Added {len(insertions)} checksum(s) to {definition}")


def write_yaml(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")


def prepare(definition, work):
    document = read_definition(definition)
    # The archive writer verifies the exact bytes it streams into the RPM,
    # avoiding a separate full-file read for large inputs.
    files = validate_files(definition, document, verify_content=False)
    scripts = validate_scripts(document)

    distro = document["distros"][0]
    distro_name = distro["name"]
    image_type = next(iter(document["image_types"]))
    defs_path = distro["defs_path"]

    defs = work / "defs"
    reset_directory(defs)
    write_yaml(defs / "distros.yaml", {"distros": document["distros"]})
    write_yaml(defs / defs_path / "imagetypes.yaml", {"image_types": document["image_types"]})

    blueprint = json.loads(json.dumps(validate_blueprint_config(document)))
    # Image Builder installs image packages, automatic customization packages,
    # then explicit Blueprint packages as separate transactions. Keep the
    # payload alone in that last transaction so its %posttrans runs last.
    blueprint["packages"] = [{"name": PAYLOAD_NAME}]
    blueprint_file = work / "blueprint.json"
    blueprint_file.write_text(json.dumps(blueprint, indent=2) + "\n", encoding="utf-8")

    rpm_root = work / "rpmbuild"
    source_dir = rpm_root / "SOURCES"
    spec_dir = rpm_root / "SPECS"
    rpm_tmp_dir = rpm_root / "tmp"
    source_dir.mkdir(parents=True, exist_ok=True)
    spec_dir.mkdir(parents=True, exist_ok=True)
    rpm_tmp_dir.mkdir(parents=True, exist_ok=True)
    source_archive = source_dir / "payload.tar.gz"
    destinations = []
    with source_archive.open("wb") as compressed:
        with gzip.GzipFile(fileobj=compressed, mode="wb", mtime=0) as gzip_stream:
            with tarfile.open(fileobj=gzip_stream, mode="w", format=tarfile.PAX_FORMAT) as archive:
                for item in files:
                    destination = canonical_destination(item["destination"], item["source"])
                    add_tar_file(archive, definition, item, destination)
                    destinations.append(destination)
                script = "#!/bin/bash\nset -euo pipefail\n" + "\n".join(scripts) + "\n"
                info = tarfile.TarInfo("payload/tree" + POSTTRANS_PATH)
                payload = script.encode("utf-8")
                info.size = len(payload)
                info.mode = 0o755
                info.uid = info.gid = 0
                info.uname = info.gname = "root"
                info.mtime = 0
                info.pax_headers = {}
                archive.addfile(info, io.BytesIO(payload))
    destinations.append(POSTTRANS_PATH)
    content_hash = digest_file(source_archive)
    spec = f"""Name: {PAYLOAD_NAME}
Version: 1
Release: 1.{content_hash}
Summary: Local files and after-install scripts for an Image Builder bootc image
License: MIT
BuildArch: noarch
Source0: payload.tar.gz
AutoReqProv: no

%description
Generated local payload for the bootc image build.

%prep
%setup -q -n payload

%build

%install
mkdir -p %{{buildroot}}
cp -a tree/. %{{buildroot}}/

%files
%defattr(-,root,root,-)
""" + "\n".join(destinations) + f"\n\n%posttrans -p /bin/bash\n{POSTTRANS_PATH}\n"
    spec_file = spec_dir / f"{PAYLOAD_NAME}.spec"
    spec_file.write_text(spec, encoding="utf-8")
    rpm_root_dir = rpm_root / "RPMS"
    if rpm_root_dir.exists():
        for old in rpm_root_dir.rglob(f"{PAYLOAD_NAME}-*.rpm"):
            old.unlink()
    run([
        "rpmbuild",
        "-bb",
        "--define",
        f"_topdir {rpm_root}",
        "--define",
        f"_tmppath {rpm_tmp_dir}",
        str(spec_file),
    ])

    rpm_output = rpm_root_dir / "noarch"
    rpms = list(rpm_output.glob(f"{PAYLOAD_NAME}-*.rpm")) if rpm_output.exists() else []
    if len(rpms) != 1:
        raise RuntimeError(f"expected one generated RPM, found {rpms}")

    repo = work / "repo"
    reset_directory(repo)
    shutil.copy2(rpms[0], repo / rpms[0].name)
    run(["createrepo_c", "--quiet", str(repo)])
    return distro_name, image_type, defs, blueprint_file, repo


def find_artifact(path, suffix):
    matches = sorted(path.rglob(f"*{suffix}")) if path.exists() else []
    if len(matches) != 1:
        raise RuntimeError(f"expected one *{suffix} under {path}; found {matches}")
    return matches[0]


def build_container(definition, work):
    distro, image_type, defs, blueprint, repo = prepare(definition, work)
    output = work / "container"
    output.mkdir(parents=True, exist_ok=True)
    run(["image-builder", "build", image_type, "--distro", distro,
         "--arch", "x86_64", "--force-defs-dir", str(defs),
         "--extra-repo", repo.as_uri(), "--blueprint", str(blueprint),
         "--output-dir", str(output), "--with-manifest", "--with-buildlog"], root=True)
    archive = find_artifact(output, ".tar")
    print(f"Container archive: {archive}")
    return archive


def convert(work):
    archive = find_artifact(work / "container", ".tar")
    run(["skopeo", "copy", f"oci-archive:{archive}",
         f"containers-storage:{CONTAINER_REF}"], root=True)
    output = work / "qcow2"
    output.mkdir(parents=True, exist_ok=True)
    run(["image-builder", "build", "qcow2", "--bootc-ref", CONTAINER_REF,
         "--bootc-default-fs", "ext4",
         "--output-dir", str(output), "--with-manifest", "--with-buildlog"], root=True)
    disk = find_artifact(output, ".qcow2")
    print(f"Bootable disk: {disk}")
    return disk


def boot(work):
    disk = find_artifact(work / "qcow2", ".qcow2")
    command = ["qemu-system-x86_64", "-snapshot", "-machine", "q35", "-accel", "tcg",
               "-m", "3072", "-smp", "2", "-nographic", "-serial", "mon:stdio",
               "-drive", f"file={disk},format=qcow2,if=virtio"]
    env = os.environ.copy()
    user_tmpdir = env.get("TMPDIR")
    if user_tmpdir:
        try:
            resolved_tmpdir = Path(user_tmpdir).resolve(strict=True)
        except OSError:
            resolved_tmpdir = None
    else:
        resolved_tmpdir = None

    # QEMU redirects its snapshot overlay from /tmp to /var/tmp, so use a
    # private directory below /tmp when the inherited setting is missing,
    # unusable, or points at /tmp itself.
    if (
        resolved_tmpdir is not None
        and resolved_tmpdir != Path("/tmp")
        and resolved_tmpdir.is_dir()
        and os.access(resolved_tmpdir, os.W_OK | os.X_OK)
    ):
        env["TMPDIR"] = str(resolved_tmpdir)
        run(command, env=env)
    else:
        with tempfile.TemporaryDirectory(prefix="ibbootc-qemu-", dir="/tmp") as tmpdir:
            env["TMPDIR"] = tmpdir
            run(command, env=env)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--definition", type=Path, default=Path("fedora44-bootc.yaml"))
    parser.add_argument("--work", type=Path, default=Path("work"))
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("pin", "check", "prepare", "build-container", "convert", "build", "boot"):
        sub.add_parser(name)
    args = parser.parse_args()
    definition = args.definition.resolve()
    work = args.work.resolve()
    if args.command == "pin":
        pin_missing(definition, read_definition(definition))
        validate_files(definition, read_definition(definition))
    elif args.command == "check":
        document = read_definition(definition)
        validate_files(definition, document)
        validate_scripts(document)
        validate_blueprint_config(document)
        print("Definition and file checksums are valid")
    elif args.command == "prepare":
        prepare(definition, work)
    elif args.command == "build-container":
        build_container(definition, work)
    elif args.command == "convert":
        convert(work)
    elif args.command == "build":
        build_container(definition, work)
        convert(work)
    elif args.command == "boot":
        boot(work)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"error: {error}", file=sys.stderr)
        sys.exit(1)
