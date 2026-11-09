"""Host-side driver: builds the images and runs every stage that executes third-party code in a container.

The host only reads git history and writes result files; subject installs, test suites and mutation tools run
inside ``pymutbench-*`` images, with the network disabled for every stage except image builds.

    python3 -m pymutbench.orchestrate build-base
    python3 -m pymutbench.orchestrate build-subject attrs
    python3 -m pymutbench.orchestrate validate attrs --jobs 3
"""

from __future__ import annotations

import argparse
import functools
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

BENCH = Path(__file__).resolve().parent.parent
BASE_IMAGE = "pymutbench-base:1"
WORK = Path(os.environ.get("PMB_WORK", str(Path.home() / ".cache" / "pymutbench")))
OUT = Path(os.environ.get("PMB_OUT_DIR", str(WORK / "out")))
# Optional CPU sets (``docker run --cpuset-cpus``, e.g. "0-3"): the native campaigns of the engine track run on
# their own cores while the other stages run beside them on theirs, so a measured time never shares a core.
CPUS = os.environ.get("PMB_CPUS", "")
ENGINE_CPUS = os.environ.get("PMB_ENGINE_CPUS", "")
# Optional memory limit of a native campaign container (``docker run --memory``, e.g. "3g"): a campaign that
# exceeds it fails inside its own container instead of the VM's out-of-memory killer choosing another process.
ENGINE_MEMORY = os.environ.get("PMB_ENGINE_MEMORY", "")
# The same for every other stage container (``PMB_MEMORY``): a mutant that exhausts memory is killed inside its own
# container (the oracle's external-kill rule then makes it unknown) instead of the VM killing another container.
MEMORY = os.environ.get("PMB_MEMORY", "")

sys.path.insert(0, str(BENCH))
from pymutbench.subjects import SUBJECTS  # noqa: E402


_SNAPSHOT_LOCK = threading.Lock()


def _code_digest(source: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(p for p in source.rglob("*") if p.is_file() and "__pycache__" not in p.parts):
        digest.update(path.relative_to(source).as_posix().encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()[:16]


def bench_snapshot() -> Path:
    """An immutable copy of the harness code, mounted as ``/bench`` by every container this process starts:
    editing the working tree while a stage runs cannot change the code the stage executes (and fingerprints).
    Named by the digest of its files and verified against it, so a reused snapshot is the code it names."""
    with _SNAPSHOT_LOCK:  # the drivers start containers from several threads
        return _snapshot()


@functools.cache
def _snapshot() -> Path:
    name = _code_digest(BENCH / "pymutbench")
    target = WORK / "snapshots" / name
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=f"{name}.", dir=target.parent))
        shutil.copytree(BENCH / "pymutbench", staging / "pymutbench", ignore=shutil.ignore_patterns("__pycache__"))
        try:
            staging.rename(target)
        except OSError:  # another process published the same snapshot first
            shutil.rmtree(staging)
    if _code_digest(target / "pymutbench") != name:
        raise SystemExit(f"{target} does not hold the code it names: remove it")
    return target


@functools.cache
def image_id(image: str) -> str:
    return sh("docker", "image", "inspect", "--format", "{{.Id}}", image).strip()


def docker_run(image: str, *mounts: str, engine: bool = False) -> list[str]:
    """``docker run`` of a stage container: removed on exit, no network, the harness snapshot at ``/bench``, the
    outputs at ``/out``, the image's identity in ``PMB_IMAGE``, on the stage's CPU set if one is set."""
    cpus = ENGINE_CPUS if engine else CPUS
    memory = ENGINE_MEMORY if engine else MEMORY
    volumes = [arg for mount in (f"{bench_snapshot()}:/bench:ro", f"{OUT}:/out", *mounts) for arg in ("-v", mount)]
    return ["docker", "run", "--rm", "--network", "none", *(["--cpuset-cpus", cpus] if cpus else []),
            *(["--memory", memory, "--memory-swap", memory] if memory else []), *volumes,
            "-e", f"PMB_IMAGE={image}@{image_id(image)}", image]


def sh(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout


def repo_dir(name: str) -> Path:
    return WORK / "repos" / name


def subject_image(name: str) -> str:
    head = sh("git", "-C", str(repo_dir(name)), "rev-parse", "--short=12", "HEAD").strip()
    return f"pymutbench-subject-{name}:{head}"


def build_base() -> None:
    subprocess.run(["docker", "build", "-t", BASE_IMAGE, str(BENCH)], check=True)


def clone(name: str) -> None:
    repo = repo_dir(name)
    if not repo.exists():
        repo.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "--quiet", f"https://github.com/{SUBJECTS[name].repo}.git", str(repo)], check=True)


def build_subject(name: str) -> str:
    clone(name)
    subject = SUBJECTS[name]
    image = subject_image(name)
    install = " ".join(f"'{arg}'" for arg in subject.install)
    dockerfile = (
        f"FROM {BASE_IMAGE}\n"
        "COPY . /work/repo\n"
        "RUN git -C /work/repo reset --hard --quiet && git -C /work/repo clean -fdxq\n"
        f"RUN uv venv -q /work/venv && cd /work/repo && uv pip install -q --python /work/venv/bin/python {install}\n"
        "RUN /work/venv/bin/python -m pip --version >/dev/null 2>&1 || true\n"
    )
    with tempfile.NamedTemporaryFile("w", suffix=".Dockerfile", delete=False) as fh:
        fh.write(dockerfile)
    subprocess.run(["docker", "build", "-q", "-t", image, "-f", fh.name, str(repo_dir(name))], check=True)
    Path(fh.name).unlink()
    return image


# The tools benchmarked in the native (engine) track and their pinned releases, as in Dockerfile.tools.
NATIVE_TOOLS: dict[str, tuple[str, ...]] = {
    "mutmut": ("mutmut==3.8.1",),
    "cosmic-ray": ("cosmic-ray==8.7.0",),
    "poodle": ("poodle==1.4.0",),
    "gremlins": ("pytest-gremlins==1.13.2",),
    "leela": ("pytest-leela==0.9.0",),
    "mutation": ("mutation==0.6.0",),
    "universalmutator": ("universalmutator==1.14.1",),
    "fest": ("fest-mutate==0.1.3", "pytest-cov"),
    "irradiate": ("irradiate==0.4.3",),
    "deployproof": ("deployproof==1.1.19",),
    "balamut": ("/wheels/balamut-0.1.2-py3-none-any.whl",),
}


def native_image(name: str) -> str:
    return subject_image(name).replace("pymutbench-subject-", "pymutbench-native-")


# Test dependencies a project's suite needs at HEAD beyond the subject's judging environment, installed into every
# tool environment and the reference one of the engine track. humanize's benchmark tests need a `benchmark`
# fixture: pytest-codspeed alone provides one that calls the function once (without pytest-benchmark, whose fixture
# calibrates many rounds, and without `--codspeed`), so they run as ordinary tests.
NATIVE_TEST_EXTRAS: dict[str, tuple[str, ...]] = {"humanize": ("pytest-codspeed",)}


def build_native(name: str) -> str:
    """The subject at HEAD in /work/run, installed editable into one environment per tool."""
    subject = SUBJECTS[name]
    install = " ".join("'-e' '.'" if arg == "." else f"'{arg}'"
                       for arg in (*subject.install, *NATIVE_TEST_EXTRAS.get(name, ())))
    lines = [f"FROM {subject_image(name)}", "COPY .wheels /wheels",
             "RUN cp -a /work/repo /work/run && git -C /work/run reset --hard -q && git -C /work/run clean -fdxq"]
    for venv, specs in NATIVE_TOOLS.items():
        tool = " ".join(f"'{spec}'" for spec in specs)
        lines.append(f"RUN cd /work/run && uv venv -q /native/{venv} && "
                     # --no-config: a project's own uv settings (attrs: a one-week cooldown on new releases)
                     # must not keep a tool's pinned release out of its environment.
                     f"uv pip install --no-config -q --python /native/{venv}/bin/python {install} {tool} pytest pytest-cov")
    # The reference environment runs the project's own pytest configuration too, which may need pytest-cov
    # (python-semver's addopts pass --cov); every tool environment above has it, and the test extras.
    extras = " ".join(f"'{spec}'" for spec in NATIVE_TEST_EXTRAS.get(name, ()))
    lines.append(f"RUN uv pip install --no-config -q --python /work/venv/bin/python pytest-cov {extras}".rstrip())
    with tempfile.NamedTemporaryFile("w", suffix=".Dockerfile", delete=False) as fh:
        fh.write("\n".join(lines) + "\n")
    image = native_image(name)
    subprocess.run(["docker", "build", "-q", "-t", image, "-f", fh.name, str(BENCH)], check=True)
    Path(fh.name).unlink()
    return image


CONFORMANCE_IMAGE = "pymutbench-conformance:1"


def fixtures() -> list[str]:
    return sorted(p.name for p in (BENCH / "conformance").iterdir() if p.is_dir() and p.name.startswith("cf_"))


def build_conformance() -> str:
    """Every fixture as a git project under /work/cf, installed editable into one environment per tool; the
    reference interpreter in /work/venv has pytest only (the oracle imports each judged copy via PYTHONPATH)."""
    names = fixtures()
    editable = " ".join(f"'-e' '/work/cf/{name}'" for name in names)
    lines = [f"FROM {BASE_IMAGE}", "COPY .wheels /wheels", "COPY conformance /work/cf",
             "RUN for d in /work/cf/cf_*; do git -C $d init -q -b main && git -C $d add -A && "
             "git -C $d commit -q -m fixture; done",
             "RUN uv venv -q /work/venv && uv pip install -q --python /work/venv/bin/python pytest"]
    for venv, specs in NATIVE_TOOLS.items():
        tool = " ".join(f"'{spec}'" for spec in specs)
        lines.append(f"RUN uv venv -q /native/{venv} && uv pip install -q --python /native/{venv}/bin/python "
                     f"{editable} {tool} pytest pytest-cov")
    with tempfile.NamedTemporaryFile("w", suffix=".Dockerfile", delete=False) as fh:
        fh.write("\n".join(lines) + "\n")
    subprocess.run(["docker", "build", "-q", "-t", CONFORMANCE_IMAGE, "-f", fh.name, str(BENCH)], check=True)
    Path(fh.name).unlink()
    return CONFORMANCE_IMAGE


def _distribution(requirement: str) -> str:
    """The normalized distribution name of a ``uv pip freeze`` line (``name==1.0``, ``name @ url``)."""
    return re.sub(r"[-_.]+", "-", re.split(r"[\s=@]", requirement, maxsplit=1)[0]).lower()


def refresh_tool(tool: str, measured: str, unpin: list[str]) -> None:
    """Install the release of ``tool`` in ``NATIVE_TOOLS`` into every image that holds an environment of it, with
    the rest of each image as measured. Each image is kept under the tag ``measured`` and rebuilt FROM it: the
    release goes into the tool's existing environment, every other installed distribution pinned to its version
    except ``unpin`` (the tool's own and the dependencies its release moves). The other environments, the reference
    one and the project trees stay as they were, so the release runs on the pytest the other tools ran on and the
    oracle judges in the environment it judged them in. A full rebuild would reinstall every environment with
    unpinned pytest."""
    from pymutbench.pipeline import ENGINE_SUBJECTS, TOOLS_IMAGE  # noqa: PLC0415

    released = {_distribution(name) for name in unpin}
    specs = " ".join(f"'{spec}'" for spec in NATIVE_TOOLS[tool])
    targets = [(TOOLS_IMAGE, f"/tools/{tool}"), (CONFORMANCE_IMAGE, f"/native/{tool}"),
               *((native_image(name), f"/native/{tool}") for name in ENGINE_SUBJECTS)]
    probe = ["docker", "run", "--rm", "--network", "none", *(["--cpuset-cpus", CPUS] if CPUS else [])]
    for image, venv in targets:
        source = f"{image.partition(':')[0]}:{measured}"
        if subprocess.run(["docker", "image", "inspect", source], capture_output=True, check=False).returncode:
            sh("docker", "tag", image, source)
        before = sh(*probe, source, "uv", "pip", "freeze", "--python", f"{venv}/bin/python").splitlines()
        pins = [line for line in before if line and not line.startswith("-e ") and _distribution(line) not in released]
        with tempfile.TemporaryDirectory() as tmp:
            context = Path(tmp)
            shutil.copytree(BENCH / ".wheels", context / ".wheels")
            (context / "pins.txt").write_text("".join(f"{pin}\n" for pin in pins), encoding="utf-8")
            # Dockerfile.tools records each generation environment in freeze.txt: it describes the new one.
            freeze = f"uv pip freeze --python {venv}/bin/python > {venv}/freeze.txt"
            (context / "Dockerfile").write_text(
                f"FROM {source}\nCOPY .wheels /wheels\nCOPY pins.txt /tmp/refresh-pins.txt\n"
                f"RUN uv pip install --no-config -q --python {venv}/bin/python -c /tmp/refresh-pins.txt {specs} "
                f"&& if [ -f {venv}/freeze.txt ]; then {freeze}; fi\n",
                encoding="utf-8")
            subprocess.run(["docker", "build", "-q", "-t", image, str(context)], check=True)
        after = sh(*probe, image, "uv", "pip", "freeze", "--python", f"{venv}/bin/python").splitlines()
        removed, added = sorted(set(before) - set(after)), sorted(set(after) - set(before))
        print(f"{image} {venv}: -{removed} +{added}", flush=True)


def run_in_subject(name: str, argv: list[str], *, network: bool = False, cpus: float = 0) -> int:
    image = subject_image(name)
    OUT.mkdir(parents=True, exist_ok=True)
    cmd = ["docker", "run", "--rm", "-v", f"{bench_snapshot()}:/bench:ro", "-v", f"{OUT}:/out"]
    if not network:
        cmd += ["--network", "none"]
    if cpus:
        cmd += ["--cpus", str(cpus)]
    cmd += [image, *argv]
    return subprocess.run(cmd, check=False).returncode


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("build-base")
    p = sub.add_parser("build-subject")
    p.add_argument("names", nargs="+")
    sub.add_parser("build-conformance")
    p = sub.add_parser("build-native")
    p.add_argument("names", nargs="+")
    p = sub.add_parser("refresh-tool", help="install a tool's release from NATIVE_TOOLS into the measured images")
    p.add_argument("tool", choices=sorted(NATIVE_TOOLS))
    p.add_argument("--measured", required=True, help="the tag that keeps the images as measured (balamut-0.1.0)")
    p.add_argument("--unpin", nargs="+", required=True,
                   help="distributions free to change: the tool's own and the dependencies its release moves")
    p = sub.add_parser("validate-all", help="validate many subjects, several containers at a time")
    p.add_argument("names", nargs="*")
    p.add_argument("--containers", type=int, default=4)
    p.add_argument("--jobs", type=int, default=3)
    p.add_argument("--stage", default="validate", help="output directory under $PMB_OUT_DIR")
    p.add_argument("--recheck", default="", help="re-validate only the candidates admitted in this stage's output")
    p.add_argument("--only", default="", help="comma-separated commit prefixes (one subject)")
    p.add_argument("--list", action="store_true", help="only write each subject's candidate list")
    p = sub.add_parser("validate")
    p.add_argument("name")
    p.add_argument("--jobs", type=int, default=3)
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--only", default="")
    p.add_argument("--max-src-lines", type=int, default=40)
    args = parser.parse_args()
    match args.cmd:
        case "build-base":
            build_base()
        case "build-subject":
            for name in args.names:
                print(build_subject(name), flush=True)
        case "build-conformance":
            print(build_conformance(), flush=True)
        case "build-native":
            for name in args.names:
                print(build_native(name), flush=True)
        case "refresh-tool":
            refresh_tool(args.tool, args.measured, args.unpin)
        case "validate-all":
            names = args.names or sorted(SUBJECTS)
            (OUT / args.stage).mkdir(parents=True, exist_ok=True)

            def one(name: str) -> tuple[str, int]:
                argv = ["python3", "/bench/pymutbench/validate.py", "--subject", name, "--jobs", str(args.jobs),
                        "--out", f"/out/{args.stage}/{name}.jsonl"]
                only = [prefix for prefix in args.only.split(",") if prefix]
                if args.recheck:
                    import json  # noqa: PLC0415

                    source = OUT / args.recheck / f"{name}.jsonl"
                    rows = [json.loads(line) for line in source.read_text(encoding="utf-8").splitlines() if line.strip()]
                    admitted = [row["commit"][:12] for row in rows if row.get("admitted")]
                    if only:  # re-validate the named admissions only
                        admitted = [commit for commit in admitted if commit.startswith(tuple(only))]
                    if not admitted:
                        return name, 0
                    only = admitted
                if only:
                    argv += ["--only", ",".join(only)]
                if args.list:
                    argv.append("--list")
                log = (OUT / args.stage / f"{name}.log").open("a", encoding="utf-8")
                cmd = [*docker_run(subject_image(name)), *argv]
                return name, subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT, check=False).returncode

            from concurrent.futures import ThreadPoolExecutor  # noqa: PLC0415

            with ThreadPoolExecutor(args.containers) as pool:
                for name, code in pool.map(one, names):
                    print(f"{name}: exit {code}", flush=True)
        case "validate":
            argv = ["python3", "/bench/pymutbench/validate.py", "--subject", args.name, "--jobs", str(args.jobs),
                    "--out", f"/out/validate/{args.name}.jsonl", "--max-src-lines", str(args.max_src_lines)]
            if args.limit:
                argv += ["--limit", str(args.limit)]
            if args.only:
                argv += ["--only", args.only]
            (OUT / "validate").mkdir(parents=True, exist_ok=True)
            raise SystemExit(run_in_subject(args.name, argv))
        case _:
            raise AssertionError(args.cmd)


if __name__ == "__main__":
    main()
