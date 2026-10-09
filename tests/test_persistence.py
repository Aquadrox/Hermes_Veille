"""Persistance des données lors d'une mise à jour : dossier de données, sauvegarde,
déploiement (garde-fous) et déplacement hors du dépôt.

Les scripts de l'hôte sont exécutés pour de vrai dans un faux dépôt jetable, avec un faux
`docker` qui journalise ses appels (aucun démon requis).
"""
import json
import os
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
HOST_SCRIPTS = ("data-dir.sh", "backup.sh", "deploy.sh", "move-data.sh")

FAKE_DOCKER = """#!/bin/sh
echo "$*" >> "$DOCKER_LOG"
case "$*" in
    "logs "*) printf '%s\\n' "$FAKE_BOOT_LOG" "[seed-veille] amorçage terminé" ;;
    "inspect "*) [ -n "$FAKE_MOUNT" ] && echo "$FAKE_MOUNT" || "$FAKE_REPO/scripts/data-dir.sh" ;;
    "compose up -d") [ -z "$FAKE_LOSE" ] || rm -f "$FAKE_LOSE" ;;
esac
exit 0
"""


@pytest.fixture
def repo(tmp_path):
    """Faux dépôt : scripts de l'hôte, .env renseigné, faux docker en tête du PATH."""
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    for name in HOST_SCRIPTS:
        shutil.copy2(REPO / "scripts" / name, root / "scripts" / name)
    (root / ".env").write_text("DEEPSEEK_API_KEY=k\nBREVO_API_KEY=k\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "docker").write_text(FAKE_DOCKER)
    (bin_dir / "docker").chmod(0o755)
    return root


def run(repo, script, *args, **env):
    full_env = {k: v for k, v in os.environ.items() if k not in ("VEILLE_DATA_DIR", "BACKUP_DIR")}
    # Par défaut, le faux conteneur monte le dossier que compose résoudrait.
    full_env.update(PATH=f"{repo.parent / 'bin'}:{os.environ['PATH']}",
                    DOCKER_LOG=str(repo.parent / "docker.log"), FAKE_BOOT_LOG="", FAKE_MOUNT="",
                    FAKE_REPO=str(repo))
    full_env.update(env)
    return subprocess.run([str(repo / "scripts" / script), *args], capture_output=True,
                          text=True, env=full_env, timeout=60)


def docker_calls(repo):
    log = repo.parent / "docker.log"
    return log.read_text().splitlines() if log.exists() else []


def make_data(root: Path) -> Path:
    """Dossier de données réaliste : une veille, deux rapports, jobs, secrets et journaux."""
    rapports = root / "veilles" / "industrie-4-0" / "rapports"
    rapports.mkdir(parents=True)
    (rapports.parent / "veille.json").write_text(json.dumps({"slug": "industrie-4-0", "job_id": "j1"}))
    for day in ("2026-09-28", "2026-10-05"):
        (rapports / f"{day}-veille.pdf").write_bytes(b"%PDF" + b"x" * 1000)
        (rapports / f"{day}-veille.md").write_text("# rapport\n")
    (root / "cron").mkdir()
    (root / "cron" / "jobs.json").write_text('{"jobs": [{"id": "j1"}]}')
    (root / ".env").write_text("SECRET=1\n")
    (root / "logs").mkdir()
    (root / "logs" / "gateway.log").write_text("bruit\n")
    return root


# ------------------------------------------------------------ data-dir.sh
@pytest.mark.parametrize("line,expected", [
    (None, "{repo}/data"),
    ("VEILLE_DATA_DIR=", "{repo}/data"),
    ("VEILLE_DATA_DIR=/srv/veille/data", "/srv/veille/data"),
    ("VEILLE_DATA_DIR=/srv/veille/data/", "/srv/veille/data"),
    ('VEILLE_DATA_DIR="/srv/avec guillemets"', "/srv/avec guillemets"),
    ("VEILLE_DATA_DIR='/srv/simples'", "/srv/simples"),
    ("VEILLE_DATA_DIR=/srv/x  # commentaire", "/srv/x"),
    ("export VEILLE_DATA_DIR=/srv/exporte", "/srv/exporte"),
    ("VEILLE_DATA_DIR=./ailleurs", "{repo}/ailleurs"),
    ("VEILLE_DATA_DIR=rel/data", "{repo}/rel/data"),
])
def test_data_dir_resolution(repo, line, expected):
    if line is not None:
        (repo / ".env").write_text(f"BREVO_API_KEY=k\n{line}\n")
    out = run(repo, "data-dir.sh")
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == expected.format(repo=repo)


def test_data_dir_environment_wins_over_env_file(repo):
    (repo / ".env").write_text("VEILLE_DATA_DIR=/depuis/env-file\n")
    assert run(repo, "data-dir.sh", VEILLE_DATA_DIR="/depuis/shell").stdout.strip() == "/depuis/shell"


# --------------------------------------------------------- docker-compose
def test_compose_never_creates_an_empty_data_dir():
    text = (REPO / "docker-compose.yml").read_text()
    assert "- ./data:/opt/data" not in text  # syntaxe courte : dossier vide créé en silence
    assert "source: ${VEILLE_DATA_DIR:-./data}" in text
    assert "create_host_path: false" in text


@pytest.mark.skipif(shutil.which("docker") is None, reason="CLI docker absente")
def test_compose_config_resolves_data_dir(tmp_path):
    for name in ("docker-compose.yml", ".env.example"):
        shutil.copy(REPO / name, tmp_path / name)
    (tmp_path / ".env").write_text("VEILLE_DATA_DIR=/srv/veille-hermes/data\n")
    out = subprocess.run(["docker", "compose", "config", "--format", "json"], cwd=tmp_path,
                         capture_output=True, text=True, timeout=60)
    if out.returncode != 0 and "compose" in out.stderr and "is not a docker command" in out.stderr:
        pytest.skip("plugin docker compose absent")
    assert out.returncode == 0, out.stderr
    service = json.loads(out.stdout)["services"]["veille"]
    [volume] = service["volumes"]
    assert volume["type"] == "bind" and volume["target"] == "/opt/data"
    assert volume["source"] == "/srv/veille-hermes/data"
    # Selon la version de compose, la valeur par défaut (false) est omise de la sortie JSON.
    assert volume.get("bind", {}).get("create_host_path", False) is False
    assert all(p["host_ip"] == "127.0.0.1" for p in service["ports"])


# ------------------------------------------------------------- backup.sh
def test_backup_next_to_data_without_secrets_and_rotated(repo, tmp_path):
    data = make_data(tmp_path / "srv" / "data")
    (data / "veilles" / ".credentials.json").write_text('{"values": {"DIGIKEY_CLIENT_SECRET": "x"}}')
    (data / "imports").mkdir()
    (data / "imports" / "bom.xlsx").write_bytes(b"nomenclature confidentielle")
    backups = tmp_path / "srv" / "backups"
    backups.mkdir()
    for i in range(3):  # anciennes archives, de la plus ancienne à la plus récente
        old = backups / f"veille-data-2026010{i}-000000.tar.gz"
        old.write_bytes(b"")
        os.utime(old, (1_700_000_000 + i, 1_700_000_000 + i))
    out = run(repo, "backup.sh", VEILLE_DATA_DIR=str(data), KEEP="2")
    assert out.returncode == 0, out.stderr
    archive = Path(out.stdout.strip().splitlines()[-1])
    assert archive.parent == backups  # à côté des données, donc hors du dépôt
    assert archive.stat().st_mode & 0o077 == 0  # réservée au propriétaire
    with tarfile.open(archive) as tar:
        names = set(tar.getnames())
    assert "./veilles/industrie-4-0/rapports/2026-10-05-veille.pdf" in names
    assert "./cron/jobs.json" in names
    assert "./.env" not in names and not any(n.startswith("./logs") for n in names)
    assert "./veilles/.credentials.json" not in names  # clés saisies dans l'interface : jamais archivées
    assert not any(n.startswith("./imports") for n in names)  # nomenclatures déposées : transitoires
    kept = sorted(p.name for p in backups.iterdir())
    assert kept == sorted([archive.name, "veille-data-20260102-000000.tar.gz"])


def test_backup_refuses_keep_zero(repo, tmp_path):
    # KEEP=0 supprimerait l'archive qui vient d'être créée.
    out = run(repo, "backup.sh", VEILLE_DATA_DIR=str(make_data(tmp_path / "d")), KEEP="0")
    assert out.returncode == 2 and not (tmp_path / "backups").exists()


def test_backup_refuses_missing_data_dir(repo, tmp_path):
    out = run(repo, "backup.sh", VEILLE_DATA_DIR=str(tmp_path / "absent"))
    assert out.returncode == 1 and "introuvable" in out.stderr


# ------------------------------------------------------------- deploy.sh
def test_deploy_refuses_to_start_on_a_missing_data_dir(repo, tmp_path):
    # Re-clone, mauvais dossier… : jamais de redémarrage silencieux sur un dossier vide.
    make_data(repo / "data")
    target = tmp_path / "srv" / "data"
    out = run(repo, "deploy.sh", "--no-build", VEILLE_DATA_DIR=str(target))
    assert out.returncode == 1
    assert "introuvable" in out.stderr and f"make move-data DEST={target}" in out.stderr
    assert not target.exists()
    assert not any(c.startswith("compose up") for c in docker_calls(repo))


def test_deploy_init_creates_data_dir_for_a_fresh_install(repo, tmp_path):
    target = tmp_path / "srv" / "data"
    out = run(repo, "deploy.sh", "--init", "--no-build", VEILLE_DATA_DIR=str(target))
    assert out.returncode == 0, out.stdout + out.stderr
    assert target.is_dir()
    calls = docker_calls(repo)
    assert "compose up -d" in calls and "compose stop" not in calls  # rien à sauvegarder


def test_deploy_init_never_hides_data_left_in_the_repo(repo, tmp_path):
    make_data(repo / "data")
    target = tmp_path / "srv" / "data"
    out = run(repo, "deploy.sh", "--init", "--no-build", VEILLE_DATA_DIR=str(target))
    assert out.returncode == 1 and f"make move-data DEST={target}" in out.stderr
    assert not target.exists()
    assert not any(c.startswith("compose up") for c in docker_calls(repo))


def test_deploy_fails_when_container_mounts_another_dir(repo, tmp_path):
    data = make_data(tmp_path / "srv" / "data")
    out = run(repo, "deploy.sh", "--no-build", VEILLE_DATA_DIR=str(data), FAKE_MOUNT=str(repo / "data"))
    assert out.returncode == 1 and "au lieu de" in out.stderr


def test_deploy_fails_when_reconcile_reports_an_error(repo, tmp_path):
    data = make_data(tmp_path / "srv" / "data")
    out = run(repo, "deploy.sh", "--no-build", VEILLE_DATA_DIR=str(data),
              FAKE_BOOT_LOG="[seed-veille] reconcile: ERREUR jobs.json illisible")
    assert out.returncode == 1
    assert "réconciliation en erreur" in out.stderr and "AVEC DES ERREURS" in out.stderr
    assert "données conservées" in out.stdout  # l'inventaire a tout de même été vérifié


def test_deploy_backs_up_with_container_stopped_then_checks_nothing_lost(repo, tmp_path):
    data = make_data(tmp_path / "srv" / "data")
    out = run(repo, "deploy.sh", "--no-build", VEILLE_DATA_DIR=str(data))
    assert out.returncode == 0, out.stdout + out.stderr
    calls = docker_calls(repo)
    assert calls.index("compose stop") < calls.index("compose up -d")
    assert len(list((tmp_path / "srv" / "backups").glob("veille-data-*.tar.gz"))) == 1
    assert "données conservées : 1 veille(s), 2 rapport(s) PDF" in out.stdout
    assert "DANS le dépôt" not in out.stderr


def test_deploy_fails_when_a_report_disappears(repo, tmp_path):
    data = make_data(tmp_path / "srv" / "data")
    lost = data / "veilles" / "industrie-4-0" / "rapports" / "2026-09-28-veille.pdf"
    out = run(repo, "deploy.sh", "--no-build", VEILLE_DATA_DIR=str(data), FAKE_LOSE=str(lost))
    assert out.returncode == 1
    assert "2 → 1 rapports PDF" in out.stderr and "tar -xzf" in out.stderr


def test_deploy_warns_when_data_lives_in_the_repo(repo):
    make_data(repo / "data")
    out = run(repo, "deploy.sh", "--no-build")
    assert out.returncode == 0, out.stdout + out.stderr
    assert "DANS le dépôt" in out.stderr and "make move-data" in out.stderr


# ----------------------------------------------------------- move-data.sh
def test_move_data_out_of_the_repo(repo, tmp_path):
    make_data(repo / "data")
    (repo / "data" / "veille").symlink_to(Path("veilles") / "industrie-4-0")  # lien v0.2
    dest = tmp_path / "srv" / "veille-hermes" / "data"
    out = run(repo, "move-data.sh", str(dest))
    assert out.returncode == 0, out.stdout + out.stderr
    assert not (repo / "data").exists()
    assert (dest / "veilles/industrie-4-0/rapports/2026-09-28-veille.pdf").is_file()
    assert (dest / "veille" / "rapports").is_dir()  # lien relatif toujours valide
    assert f"VEILLE_DATA_DIR={dest}" in (repo / ".env").read_text()
    assert run(repo, "data-dir.sh").stdout.strip() == str(dest)
    calls = docker_calls(repo)
    assert calls[0] == "compose down" and "compose up -d" in calls  # redéployé


def test_move_data_when_env_already_points_to_destination(repo, tmp_path):
    # .env réglé à la main AVANT de déplacer : deploy.sh renvoie vers move-data, qui doit agir.
    make_data(repo / "data")
    dest = tmp_path / "srv" / "data"
    (repo / ".env").write_text(f"DEEPSEEK_API_KEY=k\nBREVO_API_KEY=k\nVEILLE_DATA_DIR={dest}\n")
    out = run(repo, "move-data.sh", str(dest))
    assert out.returncode == 0, out.stdout + out.stderr
    assert (dest / "cron" / "jobs.json").is_file() and not (repo / "data").exists()


def test_move_data_refuses_leftover_partial_copy(repo, tmp_path):
    make_data(repo / "data")
    dest = tmp_path / "srv" / "data"
    (tmp_path / "srv" / "data.partial").mkdir(parents=True)
    out = run(repo, "move-data.sh", str(dest))
    assert out.returncode == 1 and "data.partial" in out.stderr and docker_calls(repo) == []


def test_move_data_replaces_existing_setting(repo, tmp_path):
    make_data(tmp_path / "old")
    (repo / ".env").write_text(f"DEEPSEEK_API_KEY=k\nBREVO_API_KEY=k\nVEILLE_DATA_DIR={tmp_path / 'old'}\n")
    dest = tmp_path / "new"
    assert run(repo, "move-data.sh", str(dest)).returncode == 0
    env_lines = [ln for ln in (repo / ".env").read_text().splitlines() if ln.startswith("VEILLE_DATA_DIR=")]
    assert env_lines == [f"VEILLE_DATA_DIR={dest}"]


@pytest.mark.parametrize("dest", ["relatif/data", "/srv/espace interdit", "/srv/x|y"])
def test_move_data_rejects_unsafe_destination(repo, dest):
    make_data(repo / "data")
    out = run(repo, "move-data.sh", dest)
    assert out.returncode == 1 and (repo / "data" / "cron" / "jobs.json").is_file()
    assert docker_calls(repo) == []


def test_move_data_never_merges_into_a_non_empty_destination(repo, tmp_path):
    make_data(repo / "data")
    dest = tmp_path / "occupe"
    dest.mkdir()
    (dest / "autre.txt").write_text("ne pas écraser")
    out = run(repo, "move-data.sh", str(dest))
    assert out.returncode == 1 and "pas vide" in out.stderr
    assert (repo / "data" / "cron" / "jobs.json").is_file() and docker_calls(repo) == []
