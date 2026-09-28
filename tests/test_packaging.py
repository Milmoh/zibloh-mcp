import zipfile
from pathlib import Path

from zibloh import packaging

from robot_fixtures import GOOD_MAIN

TEMPLATES = Path(__file__).resolve().parents[1] / "src" / "zibloh" / "templates"



def _robot(tmp_path: Path, files: dict) -> Path:
    for name, content in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content if isinstance(content, bytes) else content.encode())
    return tmp_path


def test_bundled_templates_pass_validation():
    for template in ("basic", "browser"):
        report = packaging.validate(TEMPLATES / template)
        assert report.ok, (template, report.errors)
        assert report.warnings == [], (template, report.warnings)


def test_minimal_robot_is_valid(tmp_path):
    assert packaging.validate(_robot(tmp_path, {"main.py": GOOD_MAIN})).ok


def test_missing_main_and_compile_errors(tmp_path):
    report = packaging.validate(_robot(tmp_path, {"helper.py": "def broken(:\n"}))
    assert any("main.py is missing" in e for e in report.errors)
    assert any("helper.py: does not compile" in e for e in report.errors)


def test_main_without_result_or_exit(tmp_path):
    report = packaging.validate(_robot(tmp_path, {"main.py": "print('hi')\n"}))
    assert any("final result" in e for e in report.errors)
    assert any("sys.exit(1)" in e for e in report.errors)


def test_credentials_and_secret_patterns_are_errors(tmp_path):
    report = packaging.validate(_robot(tmp_path, {
        "main.py": GOOD_MAIN + '\nKEY = "AKIAABCDEFGHIJKLMNOP"\n',
        ".env": "PASSWORD=x\n",
        "robot.env.json": "{}",
        "certs/client.pem": "x",
        "notes.txt": "zbl_" + "a" * 30,
    }))
    joined = "\n".join(report.errors)
    assert "main.py: contains what looks like a AWS access key" in joined
    assert ".env: looks like a credentials file" in joined
    assert "robot.env.json: looks like a credentials file" in joined
    assert "certs/client.pem: looks like a credentials file" in joined
    assert "notes.txt: contains what looks like a Zibloh API key" in joined


def test_session_files_warn_and_extensionless_files_fail(tmp_path):
    report = packaging.validate(_robot(tmp_path, {
        "main.py": GOOD_MAIN, "storage_state.json": "{}", "data": "x", "LICENSE": "MIT", ".gitignore": "*.log",
    }))
    assert report.errors == ["data: file without extension; rename it (e.g. .txt, .json) or remove it"]
    assert any("storage_state.json" in w for w in report.warnings)


def test_cloud_and_onprem_dependency_warnings(tmp_path):
    root = _robot(tmp_path, {
        "main.py": "import pandas, zeep\nimport helpers\n" + GOOD_MAIN,
        "helpers.py": "",
        "vendor_pkg/__init__.py": "",
        "requirements.txt": "pandas==2.2.3\nzeep>=4\n# comment\n",
    })
    cloud = packaging.validate(root, "cloud")
    assert cloud.ok
    assert any("not in the runner image and not vendored: zeep" in w for w in cloud.warnings)
    assert any("requirements.txt is not installed by the cloud runner: zeep" in w for w in cloud.warnings)
    assert not any("unpinned" in w for w in cloud.warnings)

    onprem = packaging.validate(root, "onprem")
    assert onprem.warnings == ["on-prem: unpinned requirements (pin with ==): zeep>=4"]


def test_zip_skips_local_tooling(tmp_path):
    root = _robot(tmp_path / "robot", {
        "main.py": GOOD_MAIN, "lib/util.py": "", ".venv/bin/python": "x",
        "__pycache__/main.cpython-312.pyc": "x", "chrome-profile/Default": "x", "output/mail.html": "x",
    })
    package = packaging.build_zip(root, tmp_path)
    with zipfile.ZipFile(package.path) as zf:
        assert sorted(zf.namelist()) == ["lib/util.py", "main.py"]
    assert package.files == 2 and len(package.sha256) == 64
