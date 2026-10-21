"""Preparación de los datos del usuario al arrancar: calibración base y migración de versiones anteriores."""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path

from .config import PROJECT_DIR, Config
from .db import Database
from .pipeline import CALIB_NUMBER_BOX, CALIB_ROI

log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent / "base"


def migrate_legacy_templates(cfg: Config) -> bool:
    """Las primeras versiones guardaban la calibración en `plantillas/` dentro de la carpeta de la
    app, que una actualización puede reemplazar. Se copia a los datos del usuario."""
    legacy = PROJECT_DIR / "plantillas"
    if not (legacy / "rkm_label.png").exists() or legacy.resolve() == cfg.templates_dir.resolve():
        return False
    if any(cfg.templates_dir.rglob("*.png")):
        return False  # ya hay calibración en su sitio: no se toca
    shutil.copytree(legacy, cfg.templates_dir, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns(".gitkeep"))
    log.info("Calibración anterior copiada a %s", cfg.templates_dir)
    return True


def seed_base_calibration(cfg: Config, db: Database) -> list[int]:
    """Deja puestos los dígitos ya calibrados y la zona del marcador, que son iguales para cualquier
    club (misma pantalla de EA FC), sin pisar nada que el usuario ya tenga. Solo falta marcar el
    nombre del equipo. Devuelve los dígitos copiados."""
    added = []
    digits_dir = cfg.templates_dir / "digits"
    for src in sorted((BASE_DIR / "digits").glob("*.png")):
        dst = digits_dir / src.name
        if not dst.exists():
            digits_dir.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
            added.append(int(src.stem))
    base = json.loads((BASE_DIR / "marcador.json").read_text(encoding="utf-8"))
    if db.get_meta(CALIB_ROI) is None:
        db.set_meta(CALIB_ROI, base["roi"])
    if db.get_meta(CALIB_NUMBER_BOX) is None:
        db.set_meta(CALIB_NUMBER_BOX, base["number_box"])
    return added


def prepare_user_data(cfg: Config, db: Database) -> None:
    cfg.templates_dir.mkdir(parents=True, exist_ok=True)
    migrate_legacy_templates(cfg)
    added = seed_base_calibration(cfg, db)
    if added:
        log.info("Dígitos de la calibración base incluidos: %s", added)
