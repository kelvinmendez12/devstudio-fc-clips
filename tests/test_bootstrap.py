import pytest

from rkm_clips.bootstrap import BASE_DIR, prepare_user_data
from rkm_clips.config import DEFAULT_TWITCH_CLIENT_ID, load_config
from rkm_clips.db import Database
from rkm_clips.detector import Templates, imread_gray, imwrite


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    import rkm_clips.bootstrap as bootstrap
    monkeypatch.setattr(bootstrap, "PROJECT_DIR", tmp_path / "app")  # sin calibración antigua
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "datos"))
    monkeypatch.setenv("CLIPS_DIR", str(tmp_path / "clips"))
    monkeypatch.delenv("TEMPLATES_DIR", raising=False)
    monkeypatch.delenv("TWITCH_CLIENT_ID", raising=False)
    return load_config(env_file=tmp_path / "no.env")


def test_defaults_need_no_credentials(cfg):
    assert cfg.client_id == DEFAULT_TWITCH_CLIENT_ID
    assert not hasattr(cfg, "client_secret")
    assert cfg.templates_dir == cfg.data_dir / "plantillas"


def test_new_install_gets_base_digits_and_zone_but_needs_team_label(cfg):
    db = Database(cfg.db_path)
    prepare_user_data(cfg, db)
    digits = sorted(int(p.stem) for p in (cfg.templates_dir / "digits").glob("*.png"))
    assert digits == sorted(int(p.stem) for p in (BASE_DIR / "digits").glob("*.png")) and digits
    assert db.get_meta("calib_roi") and db.get_meta("calib_number_box")
    with pytest.raises(FileNotFoundError):  # falta el nombre del equipo: lo único propio de cada club
        Templates.load(cfg.templates_dir)


def test_seeding_never_overwrites_user_calibration(cfg):
    db = Database(cfg.db_path)
    (cfg.templates_dir / "digits").mkdir(parents=True)
    mine = imread_gray(BASE_DIR / "digits" / "0.png") * 0 + 7  # un "0" propio, distinto del base
    imwrite(cfg.templates_dir / "digits" / "0.png", mine)
    db.set_meta("calib_roi", [1, 2, 3, 4])
    prepare_user_data(cfg, db)
    assert imread_gray(cfg.templates_dir / "digits" / "0.png").mean() == pytest.approx(7)
    assert db.get_meta("calib_roi") == [1, 2, 3, 4]


def test_legacy_data_dirs_are_kept_when_they_exist(tmp_path, monkeypatch):
    local, home = tmp_path / "Local", tmp_path / "Home"
    (local / "rkm-clips").mkdir(parents=True)
    (home / "Videos" / "RKM Clips").mkdir(parents=True)
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.setenv("USERPROFILE", str(home))
    for k in ("DATA_DIR", "CLIPS_DIR", "TEMPLATES_DIR"):
        monkeypatch.delenv(k, raising=False)
    cfg = load_config(env_file=tmp_path / "no.env")
    assert cfg.data_dir == local / "rkm-clips" and cfg.clips_dir == home / "Videos" / "RKM Clips"
    # En una instalación limpia se usan las carpetas con el nombre nuevo.
    (local / "rkm-clips").rmdir()
    (home / "Videos" / "RKM Clips").rmdir()
    cfg = load_config(env_file=tmp_path / "no.env")
    assert cfg.data_dir == local / "DevStudioFCClips" and cfg.clips_dir == home / "Videos" / "FC Clips"


def test_legacy_calibration_in_app_folder_is_migrated_once(cfg, tmp_path, monkeypatch):
    import rkm_clips.bootstrap as bootstrap
    app = tmp_path / "app"
    (app / "plantillas" / "digits").mkdir(parents=True)
    legacy_label = imread_gray(BASE_DIR / "digits" / "1.png")
    imwrite(app / "plantillas" / "rkm_label.png", legacy_label)
    imwrite(app / "plantillas" / "digits" / "5.png", legacy_label)
    monkeypatch.setattr(bootstrap, "PROJECT_DIR", app)
    db = Database(cfg.db_path)
    prepare_user_data(cfg, db)
    assert (cfg.templates_dir / "rkm_label.png").exists()
    assert (cfg.templates_dir / "digits" / "5.png").exists()  # el 5 propio sobrevive
    Templates.load(cfg.templates_dir)  # ya es una calibración completa
