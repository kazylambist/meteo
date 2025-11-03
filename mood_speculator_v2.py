#!/usr/bin/env python3 -*- coding: utf-8 -*-
"""
Humeur — Spéculation (v2)
- Ajoute des "mises" à échéance (3 semaines à 6 mois) et le mécanisme de "remise".
- À l'allocation initiale (1 point), l'utilisateur choisit la répartition + une échéance par actif non nul.
- Chaque mise convertit un certain nombre de points en une position verrouillée jusqu'à l'échéance.
- À l'échéance, la position est réglée en multipliant les points par (valeur_échéance / valeur_départ) et
  les points sont crédités au solde libre de l'utilisateur pour cet actif. L'utilisateur peut ensuite "remiser".

⚠️ MVP éducatif (non durci pour la prod) — prévoir CSRF/HTTPS/rate limiting, etc.
"""
from __future__ import annotations
import os
import re
import requests
from bs4 import BeautifulSoup
from datetime import datetime, date, timedelta, timezone
from typing import Optional

from flask import Flask, abort, flash, jsonify, jsonify, redirect, render_template_string, request, request, send_from_directory, url_for
from flask import render_template_string as render
from flask import current_app
from sqlalchemy.exc import IntegrityError
from sqlalchemy import text 
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import event
from sqlalchemy.engine import Engine
from flask_login import (
    LoginManager, login_user, login_required, logout_user,
    current_user, UserMixin
)
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz
from werkzeug.security import generate_password_hash, check_password_hash
from email_validator import validate_email, EmailNotValidError
from dateutil import parser as dtparse

try:
    from datetime import UTC
except ImportError:
    from datetime import timezone as _tz
    UTC = _tz.utc

import json, os
from pathlib import Path
from PIL import Image
import io

# Ordre d’empilement identique à Cabine
AVATAR_ORDER = [
    "FOND","PIEDS","TORSE","JAMBES","CEINTURE","ARME","ACCESSOIRE","TRONCHE","MASQUE","LUNETTES","CHAPEAU"
]

def _fs_path_from_web(path: str) -> str:
    """
    Convertit un chemin web (ex: '/cabine/assets/torse/Mark.png')
    vers un chemin filesystem sous app.static_folder.
    """
    if not path:
        return ""
    p = path.lstrip("/")  # 'cabine/assets/...'
    return os.path.join(app.static_folder, p)


STATIONS_PATH = Path(__file__).parent / "stations(3).json"

def load_stations():
    with open(STATIONS_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    # NOTE: le JSON fourni ne contient pas de lat/lon. On met un petit fallback
    # pour quelques icao connus. Idéalement, ajoute lat/lon dans le JSON.
    ICAO_COORDS = {
        "LFLP": (45.930, 6.106),   # Annecy
        "LFPG": (49.0097, 2.5479), # CDG
        "LFPO": (48.7262, 2.3652), # Orly
        "LFBD": (44.8283, -0.7156),# Bordeaux
        "LFMN": (43.6584, 7.2159), # Nice
        "LFML": (43.4393, 5.2214), # Marseille
        "LFLL": (45.7264, 5.0908), # Lyon
        # ...ajoute au besoin
    }
    for s in data:
        icao = s.get("icao")
        latlon = ICAO_COORDS.get(icao)
        s["lat"] = latlon[0] if latlon else None
        s["lon"] = latlon[1] if latlon else None
        s["label"] = f"{s.get('name')} — {s.get('city')} ({s.get('dept')})"
    return data

# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------
APP_TZ = pytz.timezone("Europe/Paris")
SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-change-me")
SITE_URL = os.environ.get("SITE_URL", "http://localhost:5000")
ADMIN_EMAIL = (os.environ.get("ADMIN_EMAIL") or "").strip().lower()
DB_PATH = os.environ.get("DATABASE_URL", "sqlite:///moodspec.db")

# -----------------------------------------------------------------------------
# App
# -----------------------------------------------------------------------------
app = Flask(__name__)

# --- HEALTHCHECK minimal, sans DB ni auth, sans réponse anticipée ---
from flask import Response, request

HEALTH_PATHS = ("/health", "/healthz", "/ready", "/live")

# Laisse simplement passer les requêtes santé (ne PAS répondre ici)
@app.before_request
def _bypass_filters_for_health():
    if request.path in HEALTH_PATHS:
        return None  # ne bloque pas

def _health_ok():
    return Response("ok", status=200, mimetype="text/plain")

# Enregistre 1 seule fois des endpoints uniques
for p in HEALTH_PATHS:
    ep = f"health_{p.strip('/').replace('-', '_') or 'root'}"
    if ep not in app.view_functions:
        app.add_url_rule(p, endpoint=ep, view_func=_health_ok, methods=["GET", "HEAD"])

try:
    app.config.from_object("config")
except Exception:
    # Fallback si le module config n'existe pas
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev-secret-key"),
        SQLALCHEMY_DATABASE_URI=os.environ.get(
            "DATABASE_URL",
            "sqlite:///instance/moodspec.db"  # URI relative par défaut
        ),
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
    )

# --- Normalisation robuste de l'URI SQLite (absolu + dossier existant) ---
try:
    # S'assure que le dossier instance/ existe (utilisé par Flask)
    os.makedirs(app.instance_path, exist_ok=True)

    uri = app.config.get("SQLALCHEMY_DATABASE_URI")
    if uri:
        # Si c'est une URI SQLite relative (sqlite:///chemin/relatif.db), on la rend absolue
        if uri.startswith("sqlite:///") and not uri.startswith("sqlite:////"):
            rel = uri.replace("sqlite:///", "")
            abs_path = (Path(app.root_path) / rel).resolve()
            abs_path.parent.mkdir(parents=True, exist_ok=True)
            app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{abs_path}"
    else:
        # Si aucune URI n'a été fournie, fallback propre dans instance/
        db_file = Path(app.instance_path) / "moodspec.db"
        app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{db_file}"

    # Au cas où la config ne l'aurait pas posé
    app.config.setdefault("SQLALCHEMY_TRACK_MODIFICATIONS", False)
except Exception:
    pass

@event.listens_for(Engine, "connect")
def _set_sqlite_pragma(dbapi_connection, connection_record):
    try:
        cur = dbapi_connection.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()
    except Exception:
        pass

db = SQLAlchemy(app)

# --- Flask-Migrate (migrations Alembic automatiques) ---
from flask_migrate import Migrate
migrate = Migrate(app, db)

login_manager = LoginManager(app)
login_manager.login_view = "login"

# --- HSTS (HTTPS strict) : active en prod uniquement ---
if not app.debug and not app.testing:
    @app.after_request
    def add_hsts(resp):
        # en environnement Fly, https est signalé via X-Forwarded-Proto
        if resp and (
            request.is_secure or
            request.headers.get("X-Forwarded-Proto", "http") == "https"
        ):
            resp.headers["Strict-Transport-Security"] = \
                "max-age=31536000; includeSubDomains; preload"
        return resp

import os
from sqlalchemy import text

# Exécuter les migrations idempotentes SEULEMENT si RUN_MIGRATIONS=1 (ex: en local)
RUN_MIG = os.environ.get("RUN_MIGRATIONS", "0") == "1"

if RUN_MIG:
    with app.app_context():
        try:
            # --- PPP : colonnes historiques ---
            try:
                db.session.execute(text(
                    "ALTER TABLE ppp_bet ADD COLUMN station_id VARCHAR(64)"
                ))
            except Exception:
                pass

            try:
                db.session.execute(text(
                    "ALTER TABLE ppp_boosts ADD COLUMN value REAL NOT NULL DEFAULT 0"
                ))
            except Exception:
                pass

            try:
                db.session.execute(text(
                    "ALTER TABLE ppp_boosts ADD COLUMN created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP"
                ))
            except Exception:
                pass

            # PPP: funded_from_balance pour séparer solde vs. achat
            try:
                db.session.execute(text(
                    "ALTER TABLE ppp_bet ADD COLUMN funded_from_balance INTEGER NOT NULL DEFAULT 1"
                ))
            except Exception:
                pass

            # PPP : nouvelle colonne pour la date/heure cible du pari (ex: "2025-11-15T18:00:00")
            try:
                db.session.execute(text(
                    "ALTER TABLE ppp_bet ADD COLUMN target_dt TEXT"
                ))
            except Exception:
                pass

            # Index PPP pour accélérer la page
            try:
                db.session.execute(text(
                    "CREATE INDEX IF NOT EXISTS ix_pppbet_user_status_station_date "
                    "ON ppp_bet(user_id, status, station_id, bet_date)"
                ))
            except Exception:
                pass

            # --- POSITION : rattacher à user ---
            try:
                db.session.execute(text(
                    "ALTER TABLE position ADD COLUMN user_id INTEGER REFERENCES user(id)"
                ))
            except Exception:
                pass
            try:
                db.session.execute(text(
                    "CREATE INDEX IF NOT EXISTS ix_position_user_id ON position(user_id)"
                ))
            except Exception:
                pass

            # --- CHAT : flag lecture + index ---
            try:
                db.session.execute(text(
                    "ALTER TABLE chat_messages ADD COLUMN is_read INTEGER NOT NULL DEFAULT 0"
                ))
            except Exception:
                pass
            try:
                db.session.execute(text(
                    "CREATE INDEX IF NOT EXISTS ix_chat_unread_to ON chat_messages(to_user_id, is_read)"
                ))
            except Exception:
                pass
            try:
                db.session.execute(text(
                    "CREATE INDEX IF NOT EXISTS ix_chat_from_to ON chat_messages(from_user_id, to_user_id)"
                ))
            except Exception:
                pass

            # --- USER : email unique robuste ---
            try:
                db.session.execute(text(
                    "UPDATE user SET email = lower(trim(email)) WHERE email IS NOT NULL"
                ))
            except Exception:
                pass
            try:
                db.session.execute(text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_user_email ON user(lower(email))"
                ))
            except Exception:
                pass

            # --- USER : stock d'éclairs (bolts) ---
            try:
                db.session.execute(text(
                    "ALTER TABLE user ADD COLUMN bolts INTEGER NOT NULL DEFAULT 0"
                ))
            except Exception:
                pass
            # Normalisation défensive (sans écraser des valeurs existantes)
            try:
                db.session.execute(text(
                    "UPDATE user SET bolts = COALESCE(bolts, 0)"
                ))
            except Exception:
                pass

            # --- PPP_BOOSTS : schéma normalisé + clé unique incluant station ---
            # Crée la table si absente (SQLite)
            try:
                db.session.execute(text("""
                    CREATE TABLE IF NOT EXISTS ppp_boosts (
                        id INTEGER PRIMARY KEY,
                        user_id INTEGER NOT NULL,
                        bet_date TEXT NOT NULL,              -- YYYY-MM-DD
                        station_id TEXT NOT NULL DEFAULT '', -- '' = toutes stations
                        value REAL NOT NULL DEFAULT 0,
                        created_at TEXT
                    )
                """))
            except Exception:
                pass

            # PPP boosts: normaliser station_id NULL -> '' pour cohérence avec l’UPSERT
            try:
                db.session.execute(text("UPDATE ppp_boosts SET station_id = '' WHERE station_id IS NULL"))
                db.session.commit()
            except Exception:
                db.session.rollback()
                
            # Si ancienne colonne nullable → normaliser à '' (éviter NULL dans UNIQUE)
            try:
                db.session.execute(text("""
                    UPDATE ppp_boosts SET station_id = ''
                    WHERE station_id IS NULL
                """))
            except Exception:
                pass
            # Clé unique (user_id, bet_date, station_id)
            try:
                db.session.execute(text("""
                    CREATE UNIQUE INDEX IF NOT EXISTS uq_pppboost_user_date_station
                    ON ppp_boosts(user_id, bet_date, station_id)
                """))
            except Exception:
                pass

            # --- PPP_BOOSTS : normalisation + déduplication ---
            try:
                # 1) Normalise les NULL en '' (aligne avec la clé logique côté app)
                db.session.execute(text("""
                    UPDATE ppp_boosts
                    SET station_id = ''
                    WHERE station_id IS NULL
                """))
            except Exception:
                pass

            # 2) Agrège tout dans une table temporaire
            try:
                db.session.execute(text("DROP TABLE IF EXISTS _agg_boosts"))
            except Exception:
                pass

            try:
                db.session.execute(text("""
                    CREATE TEMPORARY TABLE _agg_boosts AS
                    SELECT
                        user_id,
                        bet_date,
                        COALESCE(station_id, '') AS station_id,
                        SUM(COALESCE(value,0))   AS v,
                        MIN(created_at)          AS first_created
                    FROM ppp_boosts
                    GROUP BY user_id, bet_date, COALESCE(station_id,'')
                """))
            except Exception:
                pass

            # 3) Remplace les données par la version agrégée (1 ligne par clé)
            #    (⚠️ si tu avais des FKs référant ppp_boosts.id, dis-le : je te donnerai
            #    une variante qui conserve les ids. Par défaut ppp_boosts n’est pas référencée.)
            try:
                db.session.execute(text("DELETE FROM ppp_boosts"))
                db.session.execute(text("""
                    INSERT INTO ppp_boosts (user_id, bet_date, station_id, value, created_at)
                    SELECT user_id, bet_date, station_id, v, first_created
                    FROM _agg_boosts
                """))
            except Exception:
                pass
            
            # 4) (Re)crée l’index unique pour verrouiller l’unicité
            try:
                db.session.execute(text("""
                    CREATE UNIQUE INDEX IF NOT EXISTS uq_pppboost_user_date_station
                    ON ppp_boosts(user_id, bet_date, station_id)
                """))
            except Exception:
                pass

            # 5) Nettoie la table temporaire
            try:
                db.session.execute(text("DROP TABLE IF EXISTS _agg_boosts"))
            except Exception:
                pass

            # --- TRADE : schéma et index nécessaires ---
            # Table minimale si absente
            try:
                db.session.execute(text(
                    "CREATE TABLE IF NOT EXISTS bet_listing (id INTEGER PRIMARY KEY)"
                ))
            except Exception:
                pass

            # Colonnes de base (idempotent)
            for ddl in [
                "ALTER TABLE bet_listing ADD COLUMN user_id INTEGER",
                "ALTER TABLE bet_listing ADD COLUMN status TEXT",
                "ALTER TABLE bet_listing ADD COLUMN payload TEXT",                 # JSON en TEXT
                "ALTER TABLE bet_listing ADD COLUMN kind TEXT DEFAULT 'PPP'",      # ← manquait dans tes logs
                "ALTER TABLE bet_listing ADD COLUMN created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
                "ALTER TABLE bet_listing ADD COLUMN expires_at TIMESTAMP",
                "ALTER TABLE bet_listing ADD COLUMN city TEXT",
                "ALTER TABLE bet_listing ADD COLUMN date_label TEXT",
                "ALTER TABLE bet_listing ADD COLUMN deadline_key TEXT",
                "ALTER TABLE bet_listing ADD COLUMN choice TEXT",
                "ALTER TABLE bet_listing ADD COLUMN side TEXT",
                "ALTER TABLE bet_listing ADD COLUMN stake REAL",
                "ALTER TABLE bet_listing ADD COLUMN base_odds REAL",
                "ALTER TABLE bet_listing ADD COLUMN boosts_count INTEGER",
                "ALTER TABLE bet_listing ADD COLUMN boosts_add REAL",
                "ALTER TABLE bet_listing ADD COLUMN total_odds REAL",
                "ALTER TABLE bet_listing ADD COLUMN potential_gain REAL",
                "ALTER TABLE bet_listing ADD COLUMN ask_price REAL",               # prix demandé
                "ALTER TABLE bet_listing ADD COLUMN buyer_id INTEGER",             # acheteur
                "ALTER TABLE bet_listing ADD COLUMN sale_price REAL",              # prix payé
                "ALTER TABLE bet_listing ADD COLUMN sold_at TIMESTAMP"
            ]:
                try:
                    db.session.execute(text(ddl))
                except Exception:
                    pass

            # Index pour remaining_points() et listes
            try:
                db.session.execute(text(
                    "CREATE INDEX IF NOT EXISTS ix_betlisting_buyer_status "
                    "ON bet_listing(buyer_id, status)"
                ))
            except Exception:
                pass
            try:
                db.session.execute(text(
                    "CREATE INDEX IF NOT EXISTS ix_betlisting_user_status "
                    "ON bet_listing(user_id, status)"
                ))
            except Exception:
                pass
            try:
                db.session.execute(text(
                    "CREATE INDEX IF NOT EXISTS ix_betlisting_status_expires "
                    "ON bet_listing(status, expires_at)"
                ))
            except Exception:
                pass

            try:
                db.session.execute(text("""
                    CREATE TABLE IF NOT EXISTS art_bets (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id INTEGER NOT NULL,
                        amount REAL NOT NULL,
                        verdict TEXT NOT NULL,
                        multiplier INTEGER NOT NULL,
                        payout REAL NOT NULL DEFAULT 0,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """))
                db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_artbets_user ON art_bets(user_id)"))
                db.session.commit()
            except Exception:
                db.session.rollback()

            db.session.commit()
            print("[MIGRATIONS] OK")
        except Exception as e:
            db.session.rollback()
            print("[MIGRATIONS] ERROR:", repr(e))
else:
    # En prod (Fly) : ne pas bloquer le boot avec des DDL
    # Définis RUN_MIGRATIONS=1 ponctuellement si tu veux exécuter ces migrations au boot.
    pass
    
# -----------------------------------------------------------------------------
# Models
# -----------------------------------------------------------------------------
from sqlalchemy.orm import validates

class User(UserMixin, db.Model):
    __tablename__ = "user"
    __table_args__ = {'sqlite_autoincrement': True}  # <-- empêche la réutilisation d’IDs (SQLite)

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(40), unique=True, nullable=False)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    pw_hash = db.Column(db.String(255), nullable=False)
    email_confirmed_at = db.Column(db.DateTime, nullable=True)
    allocation_pierre = db.Column(db.Float, nullable=True)
    allocation_marie = db.Column(db.Float, nullable=True)
    allocation_locked = db.Column(db.Boolean, default=False)
    bal_pierre = db.Column(db.Float, default=0.0)
    bal_marie  = db.Column(db.Float, default=0.0)
    bolts = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(APP_TZ))

    @validates("email", "username")
    def _normalize_fields(self, key, value):
        if value is None:
            return value
        if key == "email":
            return value.strip().lower()
        if key == "username":
            return value.strip()
        return value

    @property
    def is_admin(self) -> bool:
        return self.email.lower() == ADMIN_EMAIL if ADMIN_EMAIL else False

    def get_id(self):
        return str(self.id)

    # pratique pour auth
    def set_password(self, raw: str):
        self.pw_hash = generate_password_hash(raw)

    def check_password(self, raw: str) -> bool:
        return check_password_hash(self.pw_hash, raw)

class DailyMood(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    the_date = db.Column(db.Date, unique=True, nullable=False, index=True)
    pierre_value = db.Column(db.Float, nullable=False)
    marie_value = db.Column(db.Float, nullable=False)
    published_at = db.Column(db.DateTime, default=lambda: datetime.now(APP_TZ))


class PendingMood(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    the_date = db.Column(db.Date, unique=True, nullable=False, index=True)
    pierre_value = db.Column(db.Float, nullable=False)
    marie_value = db.Column(db.Float, nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(APP_TZ))


class Position(db.Model):
    """Mise en points avec échéance.
    - asset: 'PIERRE' ou 'MARIE'
    - principal_points: nombre de points bloqués au départ
    - start_value: valeur de l'actif au moment de la mise
    - start_date: date de départ
    - maturity_date: date d'échéance (>= start_date + 21j, <= + 6 mois)
    - status: 'ACTIVE' | 'SETTLED'
    - settled_points: points crédités au règlement (si SETTLED)
    - settled_at: datetime de règlement
    """
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), index=True, nullable=False)
    asset = db.Column(db.String(10), nullable=False)  # 'PIERRE' | 'MARIE'
    principal_points = db.Column(db.Float, nullable=False)
    start_value = db.Column(db.Float, nullable=False)
    start_date = db.Column(db.Date, nullable=False)
    maturity_date = db.Column(db.Date, nullable=False)
    status = db.Column(db.String(10), default='ACTIVE', index=True)
    settled_points = db.Column(db.Float, nullable=True)
    settled_at = db.Column(db.DateTime, nullable=True)

    user_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    user = db.relationship('User', backref='positions')

class WeatherSnapshot(db.Model):
    __tablename__ = 'weather_snapshot'
    id = db.Column(db.Integer, primary_key=True)
    city_query = db.Column(db.String(120), index=True, nullable=False)   # e.g. "Paris, France"
    lat = db.Column(db.Float, nullable=False)
    lon = db.Column(db.Float, nullable=False)
    ref_date = db.Column(db.Date, nullable=False)  # the “today” the snapshot was computed for (Europe/Paris)
    sun_hours_3d = db.Column(db.Float, nullable=False)
    rain_hours_3d = db.Column(db.Float, nullable=False)
    forecast_json = db.Column(db.Text, nullable=False)  # store small JSON (5-day forecast pretty compact)
    created_at = db.Column(db.DateTime, default=lambda: dt_paris_now())

    __table_args__ = (db.UniqueConstraint('city_query', 'ref_date', name='uq_ws_city_ref'),)

class WeatherPosition(db.Model):
    __tablename__ = 'weather_position'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    asset = db.Column(db.String(10), nullable=False)  # 'SOLEIL' or 'PLUIE'
    principal_part = db.Column(db.Float, nullable=False)  # the part of the 1 weather point (e.g. 0.6)
    start_hours = db.Column(db.Float, nullable=False)     # last-3-days hours at start (per asset*)
    start_date = db.Column(db.Date, nullable=False)
    maturity_date = db.Column(db.Date, nullable=False)
    city_query = db.Column(db.String(120), nullable=False)  # city chosen for this bet
    status = db.Column(db.String(12), default='ACTIVE')     # ACTIVE / SETTLED
    settled_value = db.Column(db.Float)                     # principal_part * end_hours
    settled_at = db.Column(db.DateTime)

    user = db.relationship('User', backref=db.backref('weather_positions', lazy=True))

class PPPBet(db.Model):
    __tablename__ = 'ppp_bet'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    bet_date = db.Column(db.Date, nullable=False, index=True)   # the calendar day (Europe/Paris)
    created_at = db.Column(db.DateTime, server_default=db.func.now())
    choice = db.Column(db.String(12), nullable=False)           # 'PLUIE' or 'PAS_PLUIE'
    amount = db.Column(db.Float, nullable=False)                # points staked
    odds = db.Column(db.Float, nullable=False)                  # e.g., 1.3, 2.0, 2.7
    status = db.Column(db.String(16), nullable=False, default='ACTIVE')  # ACTIVE/SETTLED/CANCELED
    result = db.Column(db.String(12))                           # 'WIN'/'LOSE' (when settled)
    station_id = db.Column(db.String(64), index=True, nullable=True)
    locked_for_trade = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    funded_from_balance = db.Column(db.Integer, nullable=False, default=1, server_default='1')
    target_time = db.Column(db.String(5), default="18:00")
    verdict = db.Column(db.String(8))
    outcome = db.Column(db.String(16))
    observed_mm = db.Column(db.Float)
    resolved_at = db.Column(db.DateTime)

class WetBet(db.Model):
    __tablename__ = 'wet_bets'

    id          = db.Column(db.Integer, primary_key=True)
    user_id     = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)

    # Slot (hour bucket)
    slot_dt     = db.Column(db.DateTime, nullable=False, index=True)

    # Stake info
    target_pct  = db.Column(db.Integer, nullable=False)    # 0..100
    amount      = db.Column(db.Float,   nullable=False)
    odds        = db.Column(db.Float,   nullable=False)
    placed_at   = db.Column(db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))
    status      = db.Column(db.String(16), nullable=False, default='ACTIVE')  # ACTIVE/RESOLVED/CANCELED

    # Resolution fields
    observed_pct = db.Column(db.Integer)                   # observed humidity %
    outcome      = db.Column(db.String(16))                # e.g. WIN / LOSE / EXACT
    payout       = db.Column(db.Float)                     # amount * odds (*2 if EXACT)
    resolved_at  = db.Column(db.DateTime)                  # when we settled
    dismissed_at = db.Column(db.DateTime)                  # when the tile was dismissed (UI rule)

    user        = db.relationship('User', backref='wet_bets')


class HumidityObservation(db.Model):
    __tablename__ = "humidity_obs"
    id = db.Column(db.Integer, primary_key=True)
    station_id = db.Column(db.String, index=True, nullable=False)
    obs_time = db.Column(db.DateTime, index=True, nullable=False)  # UTC-aware recommended
    humidity = db.Column(db.Float, nullable=False)  # %
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))


from sqlalchemy.exc import IntegrityError
from datetime import date as _date

class UserStation(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    station_id = db.Column(db.String(64), nullable=False, index=True)
    station_label = db.Column(db.String(200), nullable=False)
    lat = db.Column(db.Float, nullable=True)
    lon = db.Column(db.Float, nullable=True)

    __table_args__ = (db.UniqueConstraint("user_id", "station_id", name="uq_user_station"),)   

from sqlalchemy.dialects.sqlite import JSON as SQLITE_JSON

class CabineSelection(db.Model):
    __tablename__ = "cabine_selection"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, index=True, nullable=False, unique=True)
    data = db.Column(SQLITE_JSON, nullable=False, default={})    

# --- Trade models ------------------------------------------------------------

# --- Chat ---

from datetime import datetime, timezone

class ChatMessage(db.Model):
    __tablename__ = "chat_messages"
    id            = db.Column(db.Integer, primary_key=True)
    from_user_id  = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    to_user_id    = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    body          = db.Column(db.Text, nullable=False)
    created_at    = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    is_read      = db.Column(db.Integer, nullable=False, server_default="0", default=0, index=True)

    __table_args__ = (
        db.Index("ix_chat_pair_time", "from_user_id", "to_user_id", "created_at"),
    )

# --- Bet ---

class BetListing(db.Model):
    __tablename__ = "bet_listing"

    id            = db.Column(db.Integer, primary_key=True)
    user_id       = db.Column(db.String, nullable=False)
    kind          = db.Column(db.String, nullable=False, default="PPP")
    payload       = db.Column(db.JSON, nullable=True)
    created_at    = db.Column(db.DateTime, default=datetime.utcnow)
    expires_at    = db.Column(db.DateTime, nullable=False)  # ta table l'exige

    status        = db.Column(db.String, nullable=False, default="OPEN")

    # --- champs "métier" qu'on remplit depuis PPP ---
    city          = db.Column(db.String, nullable=True)
    date_label    = db.Column(db.String, nullable=True)
    deadline_key  = db.Column(db.String, nullable=True)   # 'YYYY-MM-DD'
    choice        = db.Column(db.String, nullable=True)   # 'PLUIE' / 'PAS_PLUIE'
    side          = db.Column(db.String, nullable=False, default="RAIN")  # <— CRITIQUE

    stake         = db.Column(db.Float, nullable=True)
    base_odds     = db.Column(db.Float, nullable=True)
    boosts_count  = db.Column(db.Integer, nullable=True)
    boosts_add    = db.Column(db.Float, nullable=True)
    total_odds    = db.Column(db.Float, nullable=True)
    potential_gain= db.Column(db.Float, nullable=True)
    ask_price     = db.Column(db.Float)
    buyer_id      = db.Column(db.Integer)
    sale_price    = db.Column(db.Float)
    sold_at       = db.Column(db.DateTime)
    

class TradeProposal(db.Model):
    __tablename__ = "trade_proposals"
    id = db.Column(db.Integer, primary_key=True)
    listing_id = db.Column(db.Integer, db.ForeignKey("bet_listing.id"), nullable=False, index=True)
    from_user_id = db.Column(db.String(64), nullable=False, index=True)
    kind = db.Column(db.String(12), nullable=False)     # "POINTS" | "SWAP"
    data = db.Column(db.JSON, default=dict)             # e.g. {"points": 12.0} or {"listing_id": 42}
    status = db.Column(db.String(16), default="OPEN")   # OPEN | ACCEPTED | REJECTED | WITHDRAWN
    created_at = db.Column(db.DateTime, default=datetime.now(timezone.utc))


class PPPBoost(db.Model):
    __tablename__ = 'ppp_boosts'
    id         = db.Column(db.Integer, primary_key=True)
    user_id    = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    bet_date   = db.Column(db.Date, nullable=False, index=True)        # date de la tuile
    station_id = db.Column(db.String(64), index=True, nullable=True)
    value      = db.Column(db.Float, nullable=False, default=0.0)      # cumul des boosts pour ce user+date
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    # NB: SQLite can’t add this later with ALTER easily. It will exist only if table is (re)created.
    __table_args__ = (
        db.UniqueConstraint('user_id', 'bet_date', name='uq_pppboost_user_date'),
    )

# --- Ensure tables + columns exist (idempotent, SQLite-safe) ---
with app.app_context():
    db.create_all()

    from sqlalchemy import inspect, text

    def add_col_if_missing(table: str, column: str, ddl: str):
        """Add a column if it doesn't exist (SQLite-friendly). ddl is 'colname TYPE [DEFAULT ...] [NULL|NOT NULL]'."""
        insp = inspect(db.engine)
        try:
            cols = {c['name'] for c in insp.get_columns(table)}
        except Exception:
            cols = set()
        if column not in cols:
            try:
                db.session.execute(text(f"ALTER TABLE {table} ADD COLUMN {ddl}"))
                db.session.commit()
            except Exception:
                db.session.rollback()

    # ppp_boosts: make sure critical columns exist if table pre-dated the model
    add_col_if_missing('ppp_boosts', 'user_id',    'user_id INTEGER')
    add_col_if_missing('ppp_boosts', 'bet_date',   'bet_date DATE')
    add_col_if_missing('ppp_boosts', 'value',      'value FLOAT DEFAULT 0.0')
    add_col_if_missing('ppp_boosts', 'created_at', 'created_at DATETIME')

    add_col_if_missing('ppp_bet', 'target_time', 'target_time TEXT')
    add_col_if_missing('ppp_bet', 'target_time', 'target_time VARCHAR(5)')
    add_col_if_missing('ppp_bet', 'verdict', 'verdict VARCHAR(8)')
    add_col_if_missing('ppp_bet', 'outcome', 'outcome VARCHAR(16)')
    add_col_if_missing('ppp_bet', 'observed_mm', 'observed_mm FLOAT')
    add_col_if_missing('ppp_bet', 'resolved_at', 'resolved_at DATETIME')

    # wet_bets: settlement fields used by Wet logic
    add_col_if_missing('wet_bets', 'observed_pct', 'observed_pct FLOAT')
    add_col_if_missing('wet_bets', 'outcome',      'outcome VARCHAR(16)')
    add_col_if_missing('wet_bets', 'payout',       'payout FLOAT')
    add_col_if_missing('wet_bets', 'resolved_at',  'resolved_at DATETIME')
    add_col_if_missing('wet_bets', 'dismissed_at', 'dismissed_at DATETIME')

    # Optional: warn if UNIQUE is likely missing (only matters if the table existed before)
    try:
        insp = inspect(db.engine)
        uqs = [c['name'] for c in insp.get_unique_constraints('ppp_boosts')]
        if 'uq_pppboost_user_date' not in uqs:
            app.logger.warning(
                "PPPBoost UNIQUE(user_id, bet_date) not detected. "
                "Code will still upsert via fetch+increment, but consider a proper migration if you need hard uniqueness."
            )
    except Exception:
        pass

class ArtBet(db.Model):
    __tablename__ = "art_bets"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    amount = db.Column(db.Float, nullable=False)            # points misés
    verdict = db.Column(db.String(12), nullable=False)      # "WIN" | "LOSE"
    multiplier = db.Column(db.Integer, nullable=False)      # 7..14 si WIN, sinon 0
    payout = db.Column(db.Float, nullable=False, default=0) # amount * multiplier ou 0
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    user = db.relationship('User', backref=db.backref('art_bets', lazy=True))    
    
# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
@login_manager.user_loader
def load_user(user_id: str) -> Optional[User]:
    return db.session.get(User, int(user_id))


def today_paris() -> date:
    return datetime.now(APP_TZ).date()


def dt_paris_now() -> datetime:
    return datetime.now(APP_TZ)

from datetime import date, timedelta, datetime
import pytz

from zoneinfo import ZoneInfo
from flask import jsonify, request
import json as _json

def paris_now():
    return datetime.now(ZoneInfo("Europe/Paris"))

def today_paris_date() -> date:
    tz = pytz.timezone('Europe/Paris')
    return datetime.now(tz).date()

WET_ODDS = {
    **{h: 1.6 for h in range(1, 7)},    # 1-6h
    **{h: 1.8 for h in range(7, 13)},   # 7-12h
    **{h: 2.0 for h in range(13, 25)},  # 13-24h
    **{h: 2.2 for h in range(25, 37)},  # 25-36h
    **{h: 2.5 for h in range(37, 49)},  # 37-48h
}

def wet_odds_for_offset(hours_ahead: int) -> float | None:
    """Return odds for given hour offset (1..48). None if out of range."""
    return WET_ODDS.get(int(hours_ahead))

# Map offset → odds (per your spec). Offset d ∈ [0..30]
PPP_ODDS = {
    0:None,1:1.0,2:1.0,3:1.1,4:1.2,5:1.3,6:1.4,7:1.5,8:1.6,9:1.7,10:1.8,
    11:2.0,12:2.0,13:2.0,14:2.0,15:2.0,16:2.0,17:2.0,18:2.0,19:2.5,20:2.5,
    21:2.4,22:2.3,23:2.2,24:2.2,25:2.0,26:2.1,27:2.4,28:2.7,29:2.8,30:2.9,31:3.0
}

def ppp_odds_for_offset(d: int):
    return PPP_ODDS.get(d, None)

def ppp_validate_can_bet(target: date, today: date) -> tuple[bool,str|None,int|None,float|None]:
    """Return (ok, msg, offset, odds). First 3 days disabled; allow from +3..+31."""
    if target < today:
        return False, "Jour passé.", None, None
    offset = (target - today).days
    if offset <= 3:
        return False, "Mise interdite sur les 3 prochains jours.", offset, None
    if offset > 31:
        return False, "Calendrier limité à 31 jours.", offset, None
    odds = ppp_odds_for_offset(offset)
    if odds is None:
        return False, "Aucun taux disponible.", offset, None
    return True, None, offset, odds

def get_value_for(d: date, asset: str) -> Optional[float]:
    row = DailyMood.query.filter_by(the_date=d).first()
    if not row:
        return None
    return row.pierre_value if asset == 'PIERRE' else row.marie_value

def last_published_on_or_before(d: date) -> Optional[DailyMood]:
    return (DailyMood.query
            .filter(DailyMood.the_date <= d)
            .order_by(DailyMood.the_date.desc())
            .first())

def get_value_for_fallback(d: date, asset: str) -> Optional[float]:
    row = last_published_on_or_before(d)
    if not row:
        return None
    return row.pierre_value if asset == 'PIERRE' else row.marie_value

def remaining_mood_points(u: User) -> float:
    active = db.session.query(db.func.coalesce(db.func.sum(Position.principal_points), 0.0))\
        .filter(Position.user_id == u.id, Position.status == 'ACTIVE').scalar() or 0.0
    rem = 1.0 - float(active)
    return max(0.0, round(rem, 6))

def remaining_weather_points(u: User) -> float:
    active = db.session.query(db.func.coalesce(db.func.sum(WeatherPosition.principal_part), 0.0))\
        .filter(WeatherPosition.user_id == u.id, WeatherPosition.status == 'ACTIVE').scalar() or 0.0
    rem = 1.0 - float(active)
    return max(0.0, round(rem, 6))

from datetime import datetime, timedelta
from sqlalchemy import text

def observed_rain_between(station_id, date_obj, target_time, window_minutes=60):
    """
    Retourne la pluie observée (mm) dans la fenêtre centrée sur `target_time`
    pour la station donnée.
    - station_id: int ou None
    - date_obj: date
    - target_time: 'HH:MM' string
    """
    try:
        hour, minute = map(int, target_time.split(':'))
    except Exception:
        hour, minute = 15, 0

    # Créer le timestamp début/fin
    dt0 = datetime.combine(date_obj, datetime.min.time()) + timedelta(hours=hour, minutes=minute)
    dt1 = dt0 + timedelta(minutes=window_minutes)

    sql = """
        SELECT SUM(rain_mm)
        FROM rain_obs
        WHERE obs_time >= :start AND obs_time < :end
          AND (:sid IS NULL OR station_id = :sid)
    """
    val = db.session.execute(text(sql), {"sid": station_id, "start": dt0, "end": dt1}).scalar()
    return float(val or 0.0)

BUDGET_INITIAL = 500.0

from sqlalchemy import text, func
import os

def remaining_points(user):
    """
    1) Essaie d'abord de lire user.points (source de vérité) + bonus_points si présent.
    2) Sinon, retombe sur un calcul 'ledger' à partir des tables PPP/Wet/Trade/ArtBet (+ bonus_points).
    3) Optionnel: si POINTS_RECONCILE=1 et divergence détectée, retourne le ledger.
    """
    if not user or not getattr(user, "id", None):
        return 0.0
    uid = int(user.id)

    # --- (A) Lecture directe du solde ---
    points_now = None
    bonus_now  = 0.0
    try:
        points_now = db.session.execute(
            text('SELECT points FROM "user" WHERE id = :uid'),
            {"uid": uid}
        ).scalar()
        if points_now is not None:
            points_now = float(points_now)
    except Exception:
        pass

    # Bonus (colonne facultative)
    try:
        bonus_now = db.session.execute(
            text('SELECT bonus_points FROM "user" WHERE id = :uid'),
            {"uid": uid}
        ).scalar()
        bonus_now = float(bonus_now or 0.0)
    except Exception:
        bonus_now = 0.0  # colonne absente → ignore

    # --- (B) Calcul "ledger" de secours ---
    base = 500.0

    # PPP actifs financés depuis le solde
    try:
        ppp_active_funded = (
            db.session.query(func.coalesce(func.sum(PPPBet.amount), 0.0))
            .filter(
                PPPBet.user_id == uid,
                PPPBet.status == 'ACTIVE',
                func.coalesce(PPPBet.funded_from_balance, 1) == 1
            ).scalar()
        ) or 0.0
    except Exception:
        ppp_active_funded = (
            db.session.query(func.coalesce(func.sum(PPPBet.amount), 0.0))
            .filter(PPPBet.user_id == uid, PPPBet.status == 'ACTIVE')
            .scalar()
        ) or 0.0

    # Wet
    wet_active = (
        db.session.query(func.coalesce(func.sum(WetBet.amount), 0.0))
        .filter(WetBet.user_id == uid, WetBet.status == 'ACTIVE')
        .scalar()
    ) or 0.0
    wet_won = (
        db.session.query(func.coalesce(func.sum(WetBet.payout), 0.0))
        .filter(WetBet.user_id == uid, WetBet.status == 'RESOLVED')
        .scalar()
    ) or 0.0

    # Détection table Trade
    def _table_exists(name: str) -> bool:
        try:
            row = db.session.execute(
                text("SELECT name FROM sqlite_master WHERE type='table' AND name=:n"),
                {"n": name}
            ).fetchone()
            return bool(row)
        except Exception:
            return False

    tbl = "bet_listing" if _table_exists("bet_listing") else (
          "trade_listings" if _table_exists("trade_listings") else None)

    def _sum_price_generic(table: str, where_sql: str, params: dict) -> float:
        try:
            v = db.session.execute(text(f"""
                SELECT COALESCE(SUM(sale_price), 0.0)
                FROM {table}
                WHERE {where_sql}
            """), params).scalar()
            v = float(v or 0.0)
        except Exception:
            v = 0.0
        if v > 0:
            return v
        try:
            v2 = db.session.execute(text(f"""
                SELECT COALESCE(SUM(COALESCE(json_extract(payload, '$.ask_price'), 0.0)), 0.0)
                FROM {table}
                WHERE {where_sql}
            """), params).scalar()
            return float(v2 or 0.0)
        except Exception:
            return 0.0

    def _sum_trade_spent(uid_int: int) -> float:
        if not tbl:
            return 0.0
        where = "status = 'SOLD' AND CAST(buyer_id AS INTEGER) = :uid"
        return _sum_price_generic(tbl, where, {"uid": uid_int})

    def _sum_trade_earned(uid_int: int) -> float:
        if not tbl:
            return 0.0
        where_user = "status = 'SOLD' AND CAST(user_id AS INTEGER) = :uid"
        val = _sum_price_generic(tbl, where_user, {"uid": uid_int})
        if val > 0:
            return val
        where_seller = "status = 'SOLD' AND CAST(seller_id AS INTEGER) = :uid"
        return _sum_price_generic(tbl, where_seller, {"uid": uid_int})

    trade_spent  = _sum_trade_spent(uid)
    trade_earned = _sum_trade_earned(uid)

    # Art bets (Dessin) → net = SUM(payout - amount)
    try:
        art_net = float(db.session.execute(text("""
            SELECT COALESCE(SUM(payout - amount), 0.0)
            FROM art_bets
            WHERE user_id = :uid
        """), {"uid": uid}).scalar() or 0.0)
    except Exception:
        art_net = 0.0

    # --- (Bbis) Lecture du bonus_points ---
    bonus_now = 0.0
    try:
        bonus_now = db.session.execute(
            text('SELECT COALESCE(bonus_points, 0) FROM "user" WHERE id = :uid'),
            {"uid": uid}
        ).scalar() or 0.0
        bonus_now = float(bonus_now)
    except Exception:
        bonus_now = 0.0        

    ledger_points = (
        base
        - float(ppp_active_funded)
        - float(wet_active)
        + float(wet_won)
        - float(trade_spent)
        + float(trade_earned)
        + float(art_net)
        + float(bonus_now)   # ✅ intègre le bonus dans le ledger
    )
    ledger_points = max(0.0, round(ledger_points, 6))

    # --- (C) Choix de la valeur retournée ---
    # 1) Si user.points est lisible → on le retourne par défaut (source de vérité) + bonus
    if points_now is not None:
        points_with_bonus = float(points_now) + float(bonus_now)
        # 2) Réconciliation optionnelle
        if os.environ.get("POINTS_RECONCILE", "0") == "1":
            if abs(points_with_bonus - ledger_points) > 0.5:  # seuil tolérance
                return ledger_points
        return points_with_bonus

    # 3) Sinon, fallback: ledger (incluant bonus)
    return ledger_points

from datetime import datetime, date, timedelta

@app.cli.command("ppp_resolve")
def ppp_resolve():
    """Règle les PPP bets selon la pluie observée horaire."""
    today = today_paris_date()
    bets = PPPBet.query.filter(
        PPPBet.bet_date < today,
        PPPBet.status == 'ACTIVE',
        PPPBet.verdict.is_(None)
    ).all()

    threshold = 0.2  # mm — seuil de pluie

    for b in bets:
        rain = observed_rain_between(
            station_id=b.station_id,
            date_obj=b.bet_date,
            target_time=getattr(b, "target_time", "15:00") or "15:00"
        )

        observed_choice = "PLUIE" if rain >= threshold else "PAS_PLUIE"
        verdict = "WIN" if observed_choice == b.choice else "LOSE"

        b.verdict = verdict
        b.outcome = observed_choice
        b.observed_mm = rain
        b.resolved_at = datetime.utcnow()
        db.session.add(b)

    db.session.commit()
    print(f"✅ Résolu {len(bets)} PPP bets.")

# --- helper défensif: garantit user.bolts ---
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

def ensure_bolts_column():
    try:
        # Détecte la colonne
        rows = db.session.execute(text("PRAGMA table_info(user)")).all()
        cols = {r[1] for r in rows}  # (cid, name, type, notnull, dflt_value, pk)
        if "bolts" not in cols:
            # Ajoute la colonne
            db.session.execute(text("ALTER TABLE user ADD COLUMN bolts INTEGER NOT NULL DEFAULT 0"))
            # Backfill doux (tu peux mettre 5, 10, …)
            db.session.execute(text("UPDATE user SET bolts = 5 WHERE bolts IS NULL OR bolts = 0"))
            db.session.commit()
    except Exception:
        db.session.rollback()
        # On laisse la route échouer si on n'a pas pu réparer proprement
        raise

from zoneinfo import ZoneInfo
from datetime import timezone

def get_observed_humidity_paris(slot_dt, station_id: str = "cdg_07157"):
    """
    Retourne une humidité (%) pour un créneau WET 'slot_dt' (naïf, heure de Paris).
    Stratégie: cherche dans [slot, slot+59m], puis fallback dernière obs <= slot+59m.
    """
    try:
        tz_paris = ZoneInfo("Europe/Paris")
        if slot_dt.tzinfo is None:
            slot_local = slot_dt.replace(tzinfo=tz_paris)
        else:
            slot_local = slot_dt.astimezone(tz_paris)

        start_utc = slot_local.astimezone(timezone.utc)
        end_utc   = start_utc + timedelta(minutes=59, seconds=59)

        win = (HumidityObservation.query
               .filter_by(station_id=station_id)
               .filter(HumidityObservation.obs_time >= start_utc,
                       HumidityObservation.obs_time <= end_utc)
               .order_by(HumidityObservation.obs_time.asc())
               .first())
        if win:
            return float(win.humidity)

        fb = (HumidityObservation.query
              .filter_by(station_id=station_id)
              .filter(HumidityObservation.obs_time <= end_utc)
              .order_by(HumidityObservation.obs_time.desc())
              .first())
        if fb:
            return float(fb.humidity)

        return None
    except Exception as e:
        app.logger.warning("get_observed_humidity_paris failed: %s", e)
        return None

def resolve_due_wet_bets(user, now=None, station_id: str = "cdg_07157"):
    """
    Règles:
      - EXACT si observed == target_pct  -> payout = amount * odds * 2
      - WIN   si |observed - target|<=3  -> payout = amount * odds
      - LOSE  sinon                      -> payout = 0
    On ne clôture que les mises dont le créneau est déjà passé (heure entière atteinte).
    """
    if not user or not getattr(user, "id", None):
        return

    from zoneinfo import ZoneInfo
    tz_paris = ZoneInfo("Europe/Paris")

    if now is None:
        now = datetime.now(timezone.utc)

    # Heure courante au pas horaire (Paris), ramenée à 00 minutes
    now_paris = now.astimezone(tz_paris).replace(minute=0, second=0, microsecond=0)

    # Les slots sont stockés "naïfs" locaux -> on clôture ceux <= now_paris (naïf)
    due = (WetBet.query
        .filter(WetBet.user_id == user.id,
                WetBet.status == 'ACTIVE',
                WetBet.slot_dt <= now_paris.replace(tzinfo=None))
        .order_by(WetBet.slot_dt.asc())
        .all())

    if not due:
        return

    for bet in due:
        try:
            # Cherche l'observation (première >= slot)
            observed = get_observed_humidity_paris(bet.slot_dt, station_id=station_id)
            if observed is None:
                # Pas d'obs dispo -> on garde ACTIVE pour retenter plus tard
                continue

            # Décision
            target = int(bet.target_pct or 0)
            obs_pct = int(round(observed))  # on travaille à l'entier (affichage et règle)
            diff = abs(obs_pct - target)

            if obs_pct == target:
                outcome = 'EXACT'
                payout  = float(bet.amount) * float(bet.odds) * 2.0
            elif diff <= 3:
                outcome = 'WIN'
                payout  = float(bet.amount) * float(bet.odds)
            else:
                outcome = 'LOSE'
                payout  = 0.0

            # Mise à jour
            bet.observed_pct = obs_pct
            bet.outcome      = outcome
            bet.payout       = payout
            bet.status       = 'RESOLVED'
            bet.resolved_at  = datetime.now(timezone.utc)

            # Créditer l’utilisateur si gain
            if payout > 0:
                credit_points(user, payout)

        except Exception as e:
            app.logger.warning("resolve_due_wet_bets bet_id=%s failed: %s", getattr(bet, "id", "?"), e)

    db.session.commit()

# --- helpers pour l'affichage du solde ---
def user_solde(u) -> float:
    return remaining_points(u)  # on reflète le budget global restant

def format_points_fr(x: float) -> str:
    return f"{x:.1f}".replace('.', ',')

def parse_decimal(s: str):
    s = (s or "").strip().replace(',', '.')
    try:
        return float(s)
    except Exception:
        return None

def parse_int(s, default=None):
    try:
        return int(str(s).strip())
    except Exception:
        return default

import requests
import json
from datetime import timedelta

def geocode_city_openmeteo(q: str):
    # https://geocoding-api.open-meteo.com/v1/search?name=Paris, France&count=1&language=fr
    r = requests.get("https://geocoding-api.open-meteo.com/v1/search",
                     params={"name": q, "count": 1, "language": "fr", "format":"json"}, timeout=10)
    r.raise_for_status()
    data = r.json()
    if not data.get("results"):
        return None
    res = data["results"][0]
    return {"lat": res["latitude"], "lon": res["longitude"], "name": res["name"], "country": res.get("country")}

def openmeteo_daily(lat, lon, start_date, end_date):
    # We request daily sunshine_duration (minutes) and precipitation_hours. Open-Meteo returns minutes.
    # https://api.open-meteo.com/v1/forecast?latitude=..&longitude=..&daily=sunshine_duration,precipitation_hours&start_date=YYYY-MM-DD&end_date=YYYY-MM-DD&timezone=Europe%2FParis
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": lat, "longitude": lon,
        "daily": "sunshine_duration,precipitation_hours,weathercode,temperature_2m_max,temperature_2m_min",
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "timezone": "Europe/Paris"
    }
    r = requests.get(url, params=params, timeout=15)
    r.raise_for_status()
    return r.json()

def compute_last3_hours(lat, lon, ref_date):
    # last 3 full days ending at ref_date (inclusive)
    start = ref_date - timedelta(days=2)
    d = openmeteo_daily(lat, lon, start, ref_date)
    # Sum last 3 days
    sun_sec = 0.0
    rain_h  = 0.0
    daily = d.get("daily", {})
    sd = daily.get("sunshine_duration") or []
    ph = daily.get("precipitation_hours") or []

    for s in sd: sun_sec += (s or 0.0)      # secondes
    for h in ph: rain_h  += (h or 0.0)      # heures

    sun_h = round(sun_sec / 3600.0, 2)      # secondes → heures
    rain_h = round(rain_h, 2)               # déjà en heures
    return sun_h, rain_h

def forecast_5days(lat, lon, ref_date):
    end = ref_date + timedelta(days=5)
    d = openmeteo_daily(lat, lon, ref_date, end)
    # Keep only next 5 days (excluding today)
    daily = d.get("daily", {})
    result = []
    for i, day in enumerate(daily.get("time", [])):
        if i == 0:  # skip today here, page will show today's last-3-days separately
            continue
        result.append({
            "date": day,
            "sun_hours": round((daily.get("sunshine_duration",[0])[i] or 0) / 3600.0, 2),
            "rain_hours": round((daily.get("precipitation_hours",[0])[i] or 0), 2),
            "t_min": daily.get("temperature_2m_min",[None])[i],
            "t_max": daily.get("temperature_2m_max",[None])[i],
            "code": daily.get("weathercode",[None])[i],
        })
        if len(result) >= 5: break
    return result

def get_city_snapshot(city_query: str, ref_date, force_refresh: bool = False):
    """
    Return a WeatherSnapshot row for (city_query, ref_date).

    - If cached and force_refresh=False → reuse it.
    - If cached and force_refresh=True  → refresh fields in place.
    - If not cached → insert a new row, with UNIQUE guard.

    We ALWAYS store forecast_json as a dict: {"forecast5": [...]}.
    """
    # 1) Try existing cache first
    snap = (WeatherSnapshot.query
            .filter_by(city_query=city_query, ref_date=ref_date)
            .one_or_none())

    if snap and not force_refresh:
        # Safety: if older rows were stored as a list, normalize once and save.
        try:
            j = json.loads(snap.forecast_json or "null")
            if isinstance(j, list):
                snap.forecast_json = json.dumps({"forecast5": j})
                db.session.commit()
        except Exception:
            pass
        return snap

    # 2) Compute fresh data
    g = geocode_city_openmeteo(city_query)
    if not g:
        return None
    lat = float(g["lat"])
    lon = float(g["lon"])

    sun3d, rain3d = compute_last3_hours(lat, lon, ref_date)
    fc5 = forecast_5days(lat, lon, ref_date)  # <-- EXPECTED to be a list
    forecast_json = json.dumps({"forecast5": fc5})  # <-- ALWAYS a dict

    if snap:
        # 3a) Update existing row (force refresh)
        snap.lat = lat
        snap.lon = lon
        snap.sun_hours_3d = sun3d
        snap.rain_hours_3d = rain3d
        snap.forecast_json = forecast_json
        db.session.commit()
        return snap

    # 3b) Insert a new row, guarding UNIQUE (city_query, ref_date)
    snap = WeatherSnapshot(
        city_query=city_query,
        lat=lat, lon=lon,
        ref_date=ref_date,
        sun_hours_3d=sun3d,
        rain_hours_3d=rain3d,
        forecast_json=forecast_json,
    )
    db.session.add(snap)
    try:
        db.session.commit()
    except IntegrityError:
        # Another request inserted the same key; reuse that one
        db.session.rollback()
        snap = (WeatherSnapshot.query
                .filter_by(city_query=city_query, ref_date=ref_date)
                .one_or_none())
    return snap

INFOCLIMAT_CDG_URL = "https://www.infoclimat.fr/observations-meteo/temps-reel/roissy-charles-de-gaulle/07157.html"
TZ_PARIS = ZoneInfo("Europe/Paris")

def _parse_infoclimat_cdg_html(html: str):
    """
    Retourne une liste de tuples (obs_time_utc, humidity_float)
    à partir de la page HTML d'Infoclimat CDG. On tolère plusieurs mises en page.
    """
    soup = BeautifulSoup(html, "html.parser")
    rows = []

    # Heuristique 1 : table principale des relevés horaires
    tables = soup.find_all("table")
    time_re = re.compile(r"^\s*\d{1,2}[:h]\d{2}\s*$")  # ex "10:00" ou "10h00"
    pct_re = re.compile(r"(\d{1,3})\s*%")

    def parse_time_cell(txt: str, base_date_local: datetime):
        txt = txt.strip().lower().replace("h", ":")
        # "10:00" → construire datetime local (Paris) du jour
        try:
            hh, mm = txt.split(":")
            hh = int(hh); mm = int(mm)
            dt_local = base_date_local.replace(hour=hh, minute=mm, second=0, microsecond=0)
            # Si l’heure est dans le futur (par rapport à maintenant Paris), on recule d’un jour (table “depuis minuit”/changement de jour)
            now_paris = datetime.now(TZ_PARIS)
            if dt_local > now_paris + timedelta(minutes=5):
                dt_local = dt_local - timedelta(days=1)
            return dt_local
        except Exception:
            return None

    # Date locale “base” = aujourd’hui Paris (ajustée si la page porte sur la veille)
    base_date_local = datetime.now(TZ_PARIS)

    for table in tables:
        for tr in table.find_all("tr"):
            tds = tr.find_all(["td", "th"])
            if len(tds) < 3:
                continue

            # Cherche une cellule d'heure, puis une cellule d'humidité
            idx_time = None
            idx_hum = None
            for i, td in enumerate(tds):
                raw = td.get_text(" ", strip=True)
                if idx_time is None and time_re.match(raw or ""):
                    idx_time = i
                if idx_hum is None and "hum" in (raw or "").lower():
                    # parfois l'entête "Humidité" est dans un th voisin, on continue à chercher
                    idx_hum = i

            # Stratégie: si on a une “heure” et ailleurs un “NN %” on prend
            if idx_time is not None:
                # Cherche la première occurrence NN% dans la ligne
                hum_val = None
                for td in tds:
                    m = pct_re.search(td.get_text(" ", strip=True))
                    if m:
                        hum_val = float(m.group(1))
                        break

                if hum_val is not None:
                    time_txt = tds[idx_time].get_text(" ", strip=True)
                    dt_local = parse_time_cell(time_txt, base_date_local)
                    if dt_local:
                        dt_utc = dt_local.astimezone(timezone.utc)
                        rows.append((dt_utc, hum_val))

    # Dédup et tri
    uniq = {}
    for dt_utc, hum in rows:
        key = dt_utc.replace(second=0, microsecond=0)
        uniq[key] = hum  # garde la dernière occurrence
    out = sorted(uniq.items(), key=lambda x: x[0])
    return out


def ingest_infoclimat_cdg(station_id: str = "cdg_07157", timeout=15) -> int:
    """
    Télécharge la page Infoclimat CDG et insère dans humidity_obs
    (si non présent pour (station_id, obs_time)).
    Retourne le nombre d'insertions.
    """
    try:
        r = requests.get(INFOCLIMAT_CDG_URL, timeout=timeout, headers={
            "User-Agent": "Mozilla/5.0 (compatible; WetBot/1.0)"
        })
        r.raise_for_status()
        pairs = _parse_infoclimat_cdg_html(r.text)
        if not pairs:
            app.logger.warning("ingest_infoclimat_cdg: no rows parsed")
            return 0

        inserted = 0
        for obs_time_utc, hum in pairs:
            # Existe déjà ?
            exists = (HumidityObservation.query
                      .filter_by(station_id=station_id)
                      .filter(HumidityObservation.obs_time == obs_time_utc)
                      .first())
            if exists:
                continue
            db.session.add(HumidityObservation(
                station_id=station_id,
                obs_time=obs_time_utc,
                humidity=float(hum),
            ))
            inserted += 1

        db.session.commit()
        return inserted
    except Exception as e:
        app.logger.error("ingest_infoclimat_cdg failed: %s", e)
        db.session.rollback()
        return 0

def render_and_save_avatar_png(user_id: str, selections: dict) -> str:
    """
    Compose l’avatar à partir des couches sélectionnées et sauvegarde
    static/avatars/<user_id>.png. Renvoie le chemin web du PNG.
    """
    # Répertoire de sortie
    out_dir = os.path.join(app.static_folder, "avatars")
    os.makedirs(out_dir, exist_ok=True)
    out_fs = os.path.join(out_dir, f"{user_id}.png")
    out_web = f"/static/avatars/{user_id}.png"

    # Tente d’ouvrir la base (avatar) si existante côté Cabine
    base_web = "/cabine/assets/avatar.png"  # adapte si différent
    base_fs = _fs_path_from_web(base_web)
    canvas = None

    try:
        if os.path.exists(base_fs):
            base = Image.open(base_fs).convert("RGBA")
            canvas = Image.new("RGBA", base.size, (0, 0, 0, 0))
            canvas.alpha_composite(base)
        else:
            # fallback si pas d’avatar.png
            canvas = Image.new("RGBA", (1024, 1365), (0, 0, 0, 0))
    except Exception:
        # si souci d’ouverture: canvas vide
        canvas = Image.new("RGBA", (1024, 1365), (0, 0, 0, 0))

    # Empiler les couches selon l’ordre
    for key in AVATAR_ORDER:
        path = selections.get(key) or selections.get(key.upper()) or ""
        fs = _fs_path_from_web(path)
        if fs and os.path.exists(fs):
            try:
                layer = Image.open(fs).convert("RGBA")
                # Redimensionne si besoin pour matcher canvas
                if layer.size != canvas.size:
                    layer = layer.resize(canvas.size, Image.LANCZOS)
                canvas.alpha_composite(layer)
            except Exception:
                # on ignore les images non trouvées / corrompues
                pass

    # Sauvegarde en PNG
    try:
        canvas.save(out_fs, "PNG")
    except Exception:
        # en cas d’erreur disque, on ne bloque pas la sauvegarde JSON
        pass

    return out_web    

# -----------------------------------------------------------------------------
# Scheduler: publication des valeurs + règlement des positions arrivées à échéance
# -----------------------------------------------------------------------------
scheduler = BackgroundScheduler(timezone=str(APP_TZ))


def publish_today_if_pending():
    d = today_paris()
    p = PendingMood.query.filter_by(the_date=d).first()
    if not p:
        return
    r = DailyMood.query.filter_by(the_date=d).first()
    if not r:
        r = DailyMood(the_date=d, pierre_value=p.pierre_value, marie_value=p.marie_value,
                      published_at=dt_paris_now())
        db.session.add(r)
    else:
        r.pierre_value = p.pierre_value
        r.marie_value = p.marie_value
        r.published_at = dt_paris_now()
    db.session.commit()

from flask import current_app

def settle_maturities_job():
    # wrapper programmé dans APScheduler
    with current_app.app_context():
        settle_maturities_core() 


def settle_maturities():
    """Règle les positions dont l'échéance est aujourd'hui ou déjà passée,
    en utilisant la valeur publiée du jour d'échéance.
    Formule : points_final = principal_points * (valeur_échéance / valeur_départ)
    """
    d = today_paris()
    to_settle = Position.query.filter(
        Position.status == 'ACTIVE', Position.maturity_date <= d
    ).all()
    if not to_settle:
        return
    # S'assurer que la valeur du jour est publiée (sinon on ne règle pas aujourd'hui)
    today_row = DailyMood.query.filter_by(the_date=d).first()
    if not today_row:
        return
    for pos in to_settle:
        end_val = get_value_for_fallback(pos.maturity_date, pos.asset)
        if end_val is None:
            # Historique vide : impossible de régler pour l’instant.
            continue
        multiplier = end_val / pos.start_value if pos.start_value else 0.0
        final_points = round(pos.principal_points * multiplier, 6)
        user = pos.user
        if pos.asset == 'PIERRE':
            user.bal_pierre = round((user.bal_pierre or 0.0) + final_points, 6)
        else:
            user.bal_marie = round((user.bal_marie or 0.0) + final_points, 6)
        pos.status = 'SETTLED'
        pos.settled_points = final_points
        pos.settled_at = dt_paris_now()
    db.session.commit()


scheduler.add_job(publish_today_if_pending, CronTrigger(hour=10, minute=0, timezone=str(APP_TZ)), id='publish10', replace_existing=True)
scheduler.add_job(settle_maturities_job, 'cron', hour=10, minute=5)
scheduler.add_job(settle_maturities, CronTrigger(hour=10, minute=5, timezone=str(APP_TZ)), id='settle1005', replace_existing=True)
scheduler.start()

# -----------------------------------------------------------------------------
# UI (basique — réutilise le style v1)
# -----------------------------------------------------------------------------
BASE_CSS = """
<style>
:root{
  --bg:#06070c;               /* deep space */
  --bg2:#0b0f1a;
  --card-bg:rgba(17,22,36,.58);/* glassy panels */
  --card-border:rgba(255,255,255,.08);
  --text:#e8ecf2;
  --muted:#a8b0c2;
  --brand:#79e7ff;            /* cyan accent */
  --brand-2:#bb86fc;          /* violet accent */
  --good:#7ef7c0;
  --bad:#ff7a7a;
  --glow: 0 0 32px rgba(121,231,255,.25), 0 0 4px rgba(121,231,255,.6);
}

*{box-sizing:border-box}
html,body{height:100%}
body{
  margin:0;
  font: 15px/1.6 ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,Inter,"Helvetica Neue",Arial;
  color:var(--text);
  background: radial-gradient(1200px 900px at 80% -10%, #0e1430 0%, transparent 55%),
              radial-gradient(900px 700px at -10% 110%, #191f3b 0%, transparent 55%),
              linear-gradient(180deg, var(--bg), var(--bg2) 60%, var(--bg) 100%);
  overflow-x:hidden;
}

/* ——— Starfield (3 layers) ——— */
.stars, .stars:before, .stars:after{
  position:fixed; inset:0; content:""; z-index:-3; pointer-events:none;
  background-repeat:repeat;
  background-image:
    radial-gradient(2px 2px at 20px 30px, rgba(255,255,255,.7) 50%, transparent 51%),
    radial-gradient(1px 1px at 80px 120px, rgba(255,255,255,.5) 50%, transparent 51%),
    radial-gradient(1px 1px at 200px 50px, rgba(255,255,255,.35) 50%, transparent 51%);
  animation: drift linear infinite;
}
.stars{opacity:.35; background-size:300px 300px; animation-duration:120s}
.stars:before{opacity:.25; background-size:500px 500px; animation-duration:180s}
.stars:after{opacity:.15; background-size:800px 800px; animation-duration:260s}
@keyframes drift{from{transform:translate3d(0,0,0)} to{transform:translate3d(-200px,-120px,0)}}

/* ——— Layout / Nav ——— */
.container{max-width:1040px;margin:0 auto;padding:0 16px}
nav{position:sticky;top:0;backdrop-filter:saturate(120%) blur(8px);background:rgba(6,7,12,.55);border-bottom:1px solid var(--card-border);z-index:10}
nav .container{display:flex;gap:14px;align-items:center;min-height:56px}
nav .brand{font-weight:700;letter-spacing:.3px;color:var(--brand);text-decoration:none;text-shadow:var(--glow)}
nav a{color:var(--text);opacity:.9;text-decoration:none}
nav a:hover{color:var(--brand)}
.spacer{flex:1}

/* Navbar logo nav left(Wet & PPP) */
.topbar .nav-left { display:flex; align-items:center; gap:10px; }
.topbar .nav-left .topbar-logo { height:22px; width:auto; display:block; opacity:.95; }

/* Navbar logo (Wet & PPP) */
.topbar .nav-right { display:flex; align-items:center; gap:10px; }
.topbar .nav-right .topbar-logo { height:22px; width:auto; display:block; opacity:.95; }

/* ——— Cards ——— */
.grid{display:grid;grid-template-columns:1fr;gap:16px}
@media (min-width:900px){ .grid{grid-template-columns:1fr 1fr} }
.card{
  background:var(--card-bg);
  border:1px solid var(--card-border);
  border-radius:16px;
  padding:16px;
  box-shadow: 0 10px 40px rgba(0,0,0,.35), inset 0 1px 0 rgba(255,255,255,.04);
  backdrop-filter: blur(6px);
  transition: transform .2s ease, box-shadow .2s ease, border-color .2s ease;
}
.card:hover{ transform: translateY(-2px); border-color: rgba(121,231,255,.18); box-shadow: 0 16px 48px rgba(0,0,0,.45), 0 0 24px rgba(121,231,255,.08); }

h1,h2,h3{margin:8px 0 12px}
h2{font-size:20px}
h3{font-size:16px;color:var(--muted)}

/* ——— Buttons / Inputs ——— */
.btn{
  display:inline-block;padding:10px 14px;border-radius:12px;border:1px solid rgba(255,255,255,.14);
  background:linear-gradient(180deg, rgba(121,231,255,.15), rgba(121,231,255,.05));
  color:var(--text); cursor:pointer; text-decoration:none;
  box-shadow: inset 0 1px 0 rgba(255,255,255,.2), 0 0 10px rgba(121,231,255,.12);
}
.btn:hover{ border-color:rgba(121,231,255,.55); box-shadow: inset 0 1px 0 rgba(255,255,255,.25), 0 0 18px rgba(121,231,255,.25); }

input,select{
  width:100%; padding:10px 12px; border-radius:10px; border:1px solid rgba(255,255,255,.12);
  background:rgba(255,255,255,.04); color:var(--text); outline:none;
}
input:focus,select:focus{ border-color: rgba(121,231,255,.5); box-shadow: 0 0 0 3px rgba(121,231,255,.15); }

/* ——— Tables / Muted ——— */
.table{width:100%;border-collapse:collapse; font-variant-numeric: tabular-nums;}
.table th,.table td{padding:8px;border-bottom:1px solid rgba(255,255,255,.06)}
.table th{color:var(--muted);font-weight:500;text-align:left}
.muted{color:var(--muted)}

/* ——— Flash messages ——— */
.flash{margin-bottom:12px}
.flash-item{
  padding:10px 12px;border-radius:10px;margin-bottom:8px;
  background:linear-gradient(180deg, rgba(187,134,252,.15), rgba(187,134,252,.05));
  border:1px solid rgba(187,134,252,.25);
}
.card-small{
  padding:12px 14px;
  display:inline-block;
  max-width:320px;
}
.today-box{
  font-size:14px;
  line-height:1.4;
}
.today-box table{
  font-size:13px;
}
.today-box th{
  font-weight:500;
  color:var(--muted);
}
.today-box td{
  text-align:right;
}

/* --- Solde box styles --- */
.topbar {
  display: grid;
  grid-template-columns: 1fr auto 1fr; /* left | center | right */
  align-items: center;
  gap: 12px;
}
.nav-left, .nav-right { display: inline-flex; align-items: center; gap: 14px; }
.nav-center { display: flex; justify-content: center; align-items: center; flex: 1; }

.solde-box {
  display: inline-flex; align-items: center; gap: 6px;
  padding: 6px 12px; border-radius: 10px;
  background: #1a0f0f; color: #ffd95e; font-weight: 700; letter-spacing: .3px;
  border: 2px solid #ffb800;
  box-shadow:
    0 0 6px rgba(255,184,0,.7),
    0 0 14px rgba(255,77,0,.5),
    inset 0 0 8px rgba(255,184,0,.35);
  text-shadow:
    0 0 2px #ffec99,
    0 0 6px #ffb800,
    0 0 12px #ff4d00;
}
.solde-label { opacity: .9; }
.solde-value { font-variant-numeric: tabular-nums; }

.wet-target {
  position: absolute;
  top: 8px;
  right: 10px;
  font-weight: 700;
  opacity: .9;
  color: #2196f3;   /* bleu vif */
}

/* --- Pluie Pas Pluie (button & calendar) --- */
.ppp-btn{
  font-weight:700;
  border-color:#ffb800;
  background:linear-gradient(180deg, rgba(255,184,0,.18), rgba(255,184,0,.06));
  color:#ffd95e;
  text-shadow:0 0 2px #ffec99, 0 0 6px #ffb800, 0 0 12px #ff4d00;
  box-shadow: inset 0 1px 0 rgba(255,255,255,.25), 0 0 14px rgba(255,184,0,.25);
}
.ppp-btn:hover{ box-shadow: inset 0 1px 0 rgba(255,255,255,.3), 0 0 20px rgba(255,184,0,.35); }

.ppp-grid{
  display:grid; gap:10px;
  grid-template-columns: repeat(7, minmax(110px,1fr));
  min-height: 10rem;
  visibility: visible;
}
.ppp-day{
  position:relative; padding:10px; border-radius:12px;
  background:var(--card-bg);
  border:1px solid var(--card-border);
  min-height:90px; cursor:pointer;
}
.ppp-day.disabled{ cursor:not-allowed; opacity:.5; filter:grayscale(30%); }
.ppp-day .date{ font-weight:700; }
.ppp-day .odds{
  position:absolute; bottom:8px; right:8px;
  font-weight:800; color:#ffd95e;
  text-shadow:0 0 2px #ffec99, 0 0 6px #ffb800, 0 0 12px #ff4d00;
}
.ppp-day.today{ box-shadow: 0 0 0 2px #ffffff, 0 0 18px rgba(255,77,77,.35); }
.ppp-day.today.today-win{ box-shadow: 0 0 0 2px #30d158, 0 0 18px rgba(48,209,88,.35); }
.ppp-day.today.today-loss{ box-shadow: 0 0 0 2px #ff3b30, 0 0 18px rgba(255,59,48,.35); }
.ppp-day.disabled:after{
  content:"✖";
  position:absolute; inset:auto 6px 6px auto;
  font-size:20px; color:#ff4d4d; text-shadow:0 0 8px rgba(255,77,77,.6);
}

/* modal */
.ppp-modal{
  position:fixed; inset:0; display:none; place-items:center; z-index:20;
  background:rgba(0,0,0,.55); backdrop-filter:blur(6px);
}
.ppp-modal.open{ display:grid; }
.ppp-card{
  width:min(420px, 92vw);
  background:var(--card-bg); border:1px solid var(--card-border);
  border-radius:16px; padding:16px;
  box-shadow: 0 12px 50px rgba(0,0,0,.5);
}

/* Montant misé (en bas à gauche) */
.ppp-day .stake{
  position:absolute; bottom:8px; left:8px;
  font-weight:800; color:#7ef7c0;  /* vert “gain” */
  text-shadow:
    0 0 2px rgba(126,247,192,.9),
    0 0 6px rgba(126,247,192,.6),
    0 0 12px rgba(70,220,160,.5);
}

.stake-wrap{
  position:absolute; left:8px; bottom:8px;
  display:flex; flex-direction:column; align-items:flex-start; gap:4px;
}
.stake-amt{
  font-weight:800; color:#7ef7c0;
  text-shadow:
    0 0 2px rgba(126,247,192,.9),
    0 0 6px rgba(126,247,192,.6),
    0 0 12px rgba(70,220,160,.5);
}
.stake-icon{ width:18px; height:18px; display:block; }
.stake-icon svg{ width:100%; height:100%; display:block; }
.icon-drop path{ fill:#76d9ff; }
.icon-sun  circle{ fill:#ffd95e; }
.icon-sun  line{ stroke:#ffd95e; stroke-width:2; stroke-linecap:round; }

/* Icône de prévision (droite, au-dessus du multiplicateur) */
.forecast-wrap{
  position:absolute;
  right:8px;
  bottom:34px;        /* juste au-dessus de .odds (bottom:8px) */
  z-index:2;          /* passe au-dessus du fond et des grilles */
}
.forecast-icon{ width:18px; height:18px; display:block; }
.forecast-icon svg{ width:100%; height:100%; display:block; }
.forecast-drop path{ fill:#76d9ff; }
.forecast-sun  circle{ fill:#ffd95e; }
.forecast-sun  line{ stroke:#ffd95e; stroke-width:2; stroke-linecap:round; }
.ppp-day { pointer-events:auto; }
.ppp-grid .ppp-day.disabled { pointer-events: auto; }
.user-menu{ position:relative; }
.user-dropdown{
  position:absolute; right:0; top:100%;
  background:#fff; border:1px solid #ddd; border-radius:8px; padding:6px; min-width:220px;
  box-shadow:0 8px 24px rgba(0,0,0,.12);
}
.user-dropdown a, .user-dropdown button{
  display:block; width:100%; text-align:left; padding:8px 10px; background:none; border:0; cursor:pointer;
}
.submenu{ position:relative; }
.submenu-panel{
  position:absolute; left:100%; top:0;
  background:#fff; border:1px solid #ddd; border-radius:8px; padding:6px; min-width:220px;
  box-shadow:0 8px 24px rgba(0,0,0,.12);
}
button.danger{ color:#a30000; }
button.danger:hover{ background:#fff2f2; }
/* Les items du dropdown (liens) */
.user-dropdown .item {
  display:block;
  padding:8px 12px;
  color:#333;
  text-decoration:none;
  border-radius:6px;
}

/* Aspect au survol */
.user-dropdown .item:hover {
  background:#f5f5f5;
}

/* Le bouton "Options" hérite du même look */
.user-dropdown .submenu-toggle {
  background:transparent;
  border:0;
  width:100%;
  text-align:left;
  font:inherit;
  cursor:pointer;
}

/* Facultatif: la petite flèche à droite, si tu ne l’écris pas dans le HTML */
.user-dropdown .submenu-toggle::after {
  content:"▸";
  float:right;
  opacity:.6;
}
.topbar-logo-link { display:inline-flex; align-items:center; }
.topbar-logo-link:hover .topbar-logo { opacity:.85; }
/* Badge “nouveau message” avec halo vert */
.badge-unread {
  display: inline-block;
  font-weight: 700;
  font-size: .78rem;
  letter-spacing: .015em;
  padding: 1px 8px;
  border-radius: 999px;
  color: #0a0;
  background: rgba(0,160,0,.10);
  box-shadow:
    0 0 0 2px rgba(0,160,0,.14),
    0 0 10px rgba(0,160,0,.30);
  animation: haloPulse 1.6s ease-in-out infinite;
  text-decoration: none;     /* remove underline on <a> */
  cursor: pointer;
}
.badge-unread:hover { filter: brightness(1.05); }

@keyframes haloPulse {
  0%,100% { box-shadow: 0 0 0 2px rgba(0,160,0,.12), 0 0 6px rgba(0,160,0,.22); }
  50%     { box-shadow: 0 0 0 2px rgba(0,160,0,.16), 0 0 10px rgba(0,160,0,.36); }
}
body.ppp-page {
  background:
    url("/static/trade/bg.jpg") center center / cover no-repeat,
    radial-gradient(1200px 900px at 80% -10%, #0e1430 0%, transparent 55%),
    radial-gradient(900px 700px at -10% 110%, #191f3b 0%, transparent 55%),
    linear-gradient(180deg, var(--bg), var(--bg2) 60%, var(--bg) 100%);
  background-attachment: fixed;
}
#boltTool:not([data-count])::after,
#boltTool[data-count=""]::after { display:none; }
</style>
"""

INDEX_HTML = """
<!doctype html><html lang='fr'><head>
<meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'>
<title>Humeur — Spéculation</title>
{{ css|safe }}
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
</head><body>
<div class="stars"></div>
<nav>
  <div class="container topbar">
    <!-- Left: main links -->
    <div class="nav-left">
      <a class="brand" href="/">Humeur</a>
      <a href="/meteo" style="color:#ffd95e;">Météo</a>
      {% if current_user.is_authenticated %}
        <a href="/allocate">Attribuer (initial)</a>
        <a href="/stake">Remiser</a>
        {% if current_user.is_admin %}<a href="/admin">Admin</a>{% endif %}
      {% endif %}
    </div>

    <!-- Center: Solde -->
    <div class="nav-center">
      {% if current_user.is_authenticated and solde_str %}
        <div class="solde-box" title="Points restants Humeur + Météo">
          <span class="solde-label">Solde&nbsp;:</span>
          <span class="solde-value">{{ solde_str }}</span>
        </div>
      {% endif %}
    </div>

    <div class="nav-right">
      <a class="btn ppp-btn" href="/ppp" title="Zeus">Pluie Pas Pluie</a>
      {% if current_user.is_authenticated %}
        <span><strong>{{ current_user.username }}</strong></span>
        <a href="/logout">Se déconnecter</a>
      {% else %}
        <a href="/register">Créer un compte</a>
        <a href="/login">Se connecter</a>
      {% endif %}
    </div>    

    <!-- Right: auth -->
    <div class="nav-right">
      {% if current_user.is_authenticated %}
        <span><strong>{{ current_user.username }}</strong></span>
        <a href="/logout">Se déconnecter</a>
      {% else %}
        <a href="/register">Créer un compte</a>
        <a href="/login">Se connecter</a>
      {% endif %}
    </div>
  </div>
</nav>

<div class='container grid' style='margin-top:16px;'>

  <div class='card'>
    <h2>Valeurs de bonne humeur (publication ~10:00 CET)</h2>
  <div id="chartWrap" style="height:300px;">
    <canvas id="moodChart"></canvas>
  </div>
</div>

<div class='card card-small'>
  <h3 style="margin-top:0;margin-bottom:6px;">Valeurs du jour</h3>
  <div id="today" class="today-box"></div>
</div>

{% if current_user.is_authenticated %}
<div class='card'>
  <h3>Mon portefeuille</h3>
  <div id="wallet"></div>
  <h4 style='margin-top:16px;'>Positions actives</h4>
  <div id="positions"></div>
</div>
  {% endif %}
</div>

<script>
async function loadChart(){
  try {
    const r = await fetch('/api/moods');
    const raw = await r.json();
    if (!Array.isArray(raw) || raw.length === 0) return;

    // Normalize, coerce, sort
    const rows = raw
      .map(d => ({ date: String(d.date||""), pierre: +d.pierre, marie: +d.marie }))
      .filter(d => d.date && Number.isFinite(d.pierre) && Number.isFinite(d.marie))
      .sort((a,b)=> a.date.localeCompare(b.date));

    // De-duplicate by date (keep last)
    const byDate = new Map();
    for (const d of rows) byDate.set(d.date, d);
    const data = Array.from(byDate.values());

    const labels = data.map(d => d.date);
    const pierre = data.map(d => d.pierre);
    const marie  = data.map(d => d.marie);

    // Compute y-range padding (prevents jump)
    const yMin = Math.min(...pierre, ...marie);
    const yMax = Math.max(...pierre, ...marie);
    const pad  = Math.max((yMax - yMin) * 0.1, 5);

    const ctxEl = document.getElementById('moodChart');
    if (!ctxEl) return;
    const ctx = ctxEl.getContext('2d');

    // Destroy previous chart if any (avoid double init)
    if (window._moodChart) window._moodChart.destroy();

    window._moodChart = new Chart(ctx, {
      type: 'line',
      data: {
        labels,
        datasets: [
          { label: 'Pierre', data: pierre, tension: 0.25, pointRadius: 2 },
          { label: 'Marie',  data: marie,  tension: 0.25, pointRadius: 2 }
        ]
      },
      options: {
  responsive: true,
  maintainAspectRatio: false,
  animation: false,
  resizeDelay: 100,
  interaction: { mode: 'nearest', intersect: false },
  plugins: {
    legend: { labels: { color: '#e8ecf2' } },
    tooltip: { titleColor:'#e8ecf2', bodyColor:'#e8ecf2', backgroundColor:'rgba(17,22,36,.9)', borderColor:'rgba(255,255,255,.08)', borderWidth:1 }
  },
  scales: {
    x: {
      ticks: { color:'#a8b0c2', maxRotation:0, autoSkip:true },
      grid:  { color:'rgba(255,255,255,.06)' }
    },
    y: {
      ticks: { color:'#a8b0c2' },
      grid:  { color:'rgba(255,255,255,.06)' },
      beginAtZero:false,
      // keep your min/max padding logic if you added it earlier:
      // min: yMin - pad, max: yMax + pad
    }
  }
}
    });
  } catch (e) {
    console.error('[ui] loadChart error:', e);
  }
}

async function loadMe(){
  try {
    const wallet = document.getElementById('wallet');
    if(!wallet) return; // pas loggé → pas de section portefeuille
    const r = await fetch('/api/me');
    if(r.status!==200) { wallet.innerHTML = '<em>Non connecté.</em>'; return; }
    const me = await r.json();
    wallet.innerHTML = `<table class='table'>
      <tr><th>Solde libre Pierre</th><td><strong>${(+me.bal_pierre).toFixed(6)}</strong></td></tr>
      <tr><th>Solde libre Marie</th><td><strong>${(+me.bal_marie).toFixed(6)}</strong></td></tr>
    </table>`;
    const p = document.getElementById('positions');
    if(!p) return;
    if(!me.positions || me.positions.length===0){
      p.innerHTML = '<em>Aucune position active.</em>'; return;
    }
    p.innerHTML = '<table class="table"><tr><th>Actif</th><th>Points</th><th>Départ</th><th>Val. départ</th><th>Échéance</th><th>Statut</th></tr>' +
      me.positions.map(x => `<tr>
        <td>${x.asset}</td><td>${x.principal_points}</td>
        <td>${x.start_date}</td><td>${x.start_value}</td>
        <td>${x.maturity_date}</td><td>${x.status}</td>
      </tr>`).join('') + '</table>';
  } catch (e) {
    console.error('[ui] loadMe error:', e);
  }
}

// Ordre: aujourd'hui, portefeuille, puis graphe
(async () => {
  try { await loadToday(); } catch(e){ console.error(e); }
  try { await loadMe(); }    catch(e){ console.error(e); }
  try { await loadChart(); } catch(e){ console.error(e); }
})();
</script>
</body></html>
"""

INTRO_HTML = """
<!doctype html><html lang='fr'><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Meteo God — intro</title>
{{ css|safe }}
<style>
  :root { --introDur: 2000ms; } /* 2s */

  html, body {
    height:100%; margin:0; padding:0; overflow:hidden;
    background:#0b0f1a;
  }

  .intro-wrap {
    position:relative;
    height:100%;
    display:flex; align-items:center; justify-content:center;
  }

  /* Haze / fog made of 2 animated layers */
  .fog, .fog::before, .fog::after {
    position:absolute; inset:0; content:"";
  }
  .fog {
    filter: blur(8px);
    opacity:.75;
  }
  /* Layer A */
  .fog::before {
    background:
      radial-gradient(60vmax 60vmax at 20% 30%, rgba(255,255,255,.06), transparent 60%),
      radial-gradient(50vmax 50vmax at 80% 70%, rgba(255,255,255,.05), transparent 60%),
      radial-gradient(70vmax 70vmax at 40% 80%, rgba(255,255,255,.04), transparent 60%),
      radial-gradient(40vmax 40vmax at 70% 25%, rgba(255,255,255,.05), transparent 60%);
    animation: driftA 18s linear infinite;
  }
  /* Layer B (slower / different direction) */
  .fog::after {
    background:
      radial-gradient(55vmax 55vmax at 30% 60%, rgba(255,255,255,.05), transparent 60%),
      radial-gradient(60vmax 60vmax at 75% 35%, rgba(255,255,255,.05), transparent 60%),
      radial-gradient(45vmax 45vmax at 50% 10%, rgba(255,255,255,.04), transparent 60%),
      radial-gradient(65vmax 65vmax at 10% 80%, rgba(255,255,255,.03), transparent 60%);
    animation: driftB 26s linear infinite reverse;
  }
  @keyframes driftA { 
    0% { transform: translate3d(-6%, -3%, 0) scale(1.02); }
    50%{ transform: translate3d( 4%,  3%, 0) scale(1.03); }
    100%{transform: translate3d(-6%, -3%, 0) scale(1.02); }
  }
  @keyframes driftB { 
    0% { transform: translate3d(6%, 2%, 0) scale(1.02); }
    50%{ transform: translate3d(-4%, -2%, 0) scale(1.04); }
    100%{transform: translate3d(6%, 2%, 0) scale(1.02); }
  }

  /* Logo */
  .intro-logo {
    position:relative;
    width:min(82vw, 760px);
    max-width:760px;
    height:auto;
    z-index:2;
    filter: drop-shadow(0 10px 30px rgba(0,0,0,.4));
    animation: popIn .6s ease-out both;
  }
  @keyframes popIn {
    0% { transform: translateY(10px) scale(.96); opacity:0; }
    100%{ transform: translateY(0) scale(1); opacity:1; }
  }

  /* Small helper text (optional) */
  .intro-note{
    position:absolute; bottom:24px; width:100%; text-align:center;
    color:#a8b0c2; font-size:14px; letter-spacing:.3px;
    opacity:.8;
  }
</style>
</head>
<body>

<div class="intro-wrap">
  <div class="fog"></div>

  <img class="intro-logo"
       src="{{ url_for('static', filename='img/weather_bets_intro.png') }}"
       alt="METEO GOD — Weather bets">

  <div class="intro-note">Chargement…</div>
</div>

<script>
  // Sécurité: si l'utilisateur revient en arrière, éviter de rester bloqué sur l’intro
  setTimeout(function(){
    window.location.replace("{{ url_for('ppp') }}");
  }, 2000); // 2 secondes
</script>
</body></html>
"""

ALLOCATE_HTML = """
<!doctype html><html lang='fr'><head>
<meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'>
<title>Attribuer mon point</title>
{{ css|safe }}
</head><body>
<div class="stars"></div>
<nav>
  <div class="container topbar">
    <!-- Left: main links -->
    <div class="nav-left">
      <a class="brand" href="/">Humeur</a>
      <a href="/meteo" style="color:#ffd95e;">Météo</a>
      {% if current_user.is_authenticated %}
        <a href="/allocate">Attribuer (initial)</a>
        <a href="/stake">Remiser</a>
        {% if current_user.is_admin %}<a href="/admin">Admin</a>{% endif %}
      {% endif %}
    </div>

    <!-- Center: Solde -->
    <div class="nav-center">
      {% if current_user.is_authenticated and solde_str %}
        <div class="solde-box" title="Points restants Humeur + Météo">
          <span class="solde-label">Solde&nbsp;:</span>
          <span class="solde-value">{{ solde_str }}</span>
        </div>
      {% endif %}
    </div>

    <div class="nav-right">
      <a class="btn ppp-btn" href="/ppp" title="MeteoGod calendar">Pluie Pas Pluie</a>
      {% if current_user.is_authenticated %}
        <span><strong>{{ current_user.username }}</strong></span>
        <a href="/logout">Se déconnecter</a>
      {% else %}
        <a href="/register">Créer un compte</a>
        <a href="/login">Se connecter</a>
      {% endif %}
    </div>    

    <!-- Right: auth -->
    <div class="nav-right">
      {% if current_user.is_authenticated %}
        <span><strong>{{ current_user.username }}</strong></span>
        <a href="/logout">Se déconnecter</a>
      {% else %}
        <a href="/register">Créer un compte</a>
        <a href="/login">Se connecter</a>
      {% endif %}
    </div>
  </div>
</nav>

<div class='container' style='margin-top:16px;'>
  {% with messages = get_flashed_messages() %}
    {% if messages %}
      <div class="flash">{% for m in messages %}<div class="flash-item">{{ m }}</div>{% endfor %}</div>
    {% endif %}
  {% endwith %}

  <div class='card' style='max-width:860px;margin:0 auto;'>
    <!-- ========================= H U M E U R ========================= -->
    <h2>Attribuer mon point — Humeur</h2>
    <p>Vous disposez de <strong>1,0 point</strong>. Répartissez-le entre Pierre et Marie (somme = 1,0) et choisissez une échéance (semaines) pour chaque part &gt; 0.</p>
    <form method="post" action="/allocate">
      <input type="hidden" name="form_kind" value="mood">
      <div class='grid' style='grid-template-columns:1fr 1fr;gap:12px;'>
        <div>
          <label>Part pour Pierre (ex 0,6)</label>
          <input name="ap" type="text" inputmode="decimal" value="0.5">
        </div>
        <div>
          <label>Échéance Pierre (semaines, 3 à 24)</label>
          <input name="wp" type="number" min="3" max="24" step="1" value="3">
        </div>
        <div>
          <label>Part pour Marie (ex 0,4)</label>
          <input name="am" type="text" inputmode="decimal" value="0.5">
        </div>
        <div>
          <label>Échéance Marie (semaines, 3 à 24)</label>
          <input name="wm" type="number" min="3" max="24" step="1" value="3">
        </div>
      </div>
      <div style='margin-top:12px;'>
        <button class="btn" type="submit">Enregistrer (humeur)</button>
      </div>
    </form>

    <hr style="border-color:rgba(255,255,255,.08);margin:18px 0">

    <!-- ========================= M É T É O ========================= -->
    <hr style="border-color:rgba(255,255,255,.08);margin:18px 0">

    <h2 class="meteo-title" style="margin:0 0 8px">Attribuer mon point — Météo</h2>
    <p>Choisissez une ville, puis répartissez <strong>1,0 point</strong> entre <em>soleil</em> et <em>pluie</em> (somme = 1,0). L’échéance va de 2 à 24 semaines.</p>

    <form method="post" action="/allocate" id="weatherForm">
      <input type="hidden" name="form_kind" value="weather">

      <div class='grid' style='grid-template-columns:1fr auto;gap:12px;'>
        <div>
          <label>Choisir une ville</label>
          <input name="wcity" type="text" placeholder="Paris, France" value="Paris, France">
        </div>
        <div style="align-self:end">
          <button class="btn" type="button" id="btnCheckCity">Vérifier la ville</button>
        </div>
      </div>

  <div id="cityInfo" class="muted" style="margin:8px 0 12px;"></div>

  <div class='grid' style='grid-template-columns:1fr 1fr;gap:12px'>
    <div>
      <label>Part soleil (ex 0,5)</label>
      <input name="ws" type="text" inputmode="decimal" value="0.5">
    </div>
    <div>
      <label>Échéance soleil (semaines, 2 à 24)</label>
      <input name="wss" type="number" min="2" max="24" step="1" value="2">
    </div>
    <div>
      <label>Part pluie (ex 0,5)</label>
      <input name="wr" type="text" inputmode="decimal" value="0.5">
    </div>
    <div>
      <label>Échéance pluie (semaines, 2 à 24)</label>
      <input name="wrs" type="number" min="2" max="24" step="1" value="2">
    </div>
  </div>
  <div style='margin-top:12px;'>
    <button class="btn" type="submit">Enregistrer (météo)</button>
  </div>
</form>
  </div>
</div>

<script>
// Normaliser les décimales FR → EN sur les deux formulaires
document.addEventListener('DOMContentLoaded', () => {
  // Humeur form
  const moodForm = document.querySelector('form[action="/allocate"][method="post"]:not(#weatherForm)');
  if (moodForm) {
    moodForm.addEventListener('submit', () => {
      for (const name of ['ap','am']) {
        const el = moodForm.querySelector(`[name="\\${name}"]`);
        if (el && typeof el.value === 'string') el.value = el.value.replace(',', '.');
      }
    });
  }
  // Météo form
  const wForm = document.getElementById('weatherForm');
  if (wForm) {
    wForm.addEventListener('submit', () => {
      for (const name of ['ws','wr']) {
        const el = wForm.querySelector(`[name="${name}"]`);
        if (el && typeof el.value === 'string') el.value = el.value.replace(',', '.');
      }
    });
  }

  // Vérifier la ville (afficher heures 3j)
  const btn = document.getElementById('btnCheckCity');
  if(btn){
    btn.addEventListener('click', async ()=>{
      const inp = document.querySelector('input[name="wcity"]');
      const q = ((inp && inp.value) ? inp.value : '').trim();
      const box = document.getElementById('cityInfo');
      if(!q){ box.textContent='Saisissez une ville.'; return; }
      box.textContent='Chargement…';
      try{
        const t = await fetch('/api/meteo/today?city='+encodeURIComponent(q)).then(r=>r.json());
        if(t.error){ box.textContent='Ville introuvable.'; return; }
        box.innerHTML = `Ville: <strong>${t.city}</strong> — ${t.date}<br>
          Soleil (3j): <strong>${t.sun_hours_3d}</strong> h — Pluie (3j): <strong>${t.rain_hours_3d}</strong> h`;
      }catch(e){ box.textContent='Erreur de récupération.' }
    });
  }
});
</script>
</body></html>
"""

AUTH_HTML = """
<!doctype html><html lang='fr'><head>
<meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'>
<link rel="icon" href="{{ url_for('static', filename='img/favicon.ico') }}?v=2" type="image/x-icon">
<link rel="icon" type="image/png" sizes="32x32" href="{{ url_for('static', filename='img/favicon-32.png') }}?v=2">
<link rel="icon" type="image/png" sizes="16x16" href="{{ url_for('static', filename='img/favicon-16.png') }}?v=2">
<link rel="shortcut icon" href="{{ url_for('static', filename='img/favicon.ico') }}?v=2">
<link rel="apple-touch-icon" sizes="180x180" href="{{ url_for('static', filename='img/apple-touch-icon.png') }}?v=2">
{{ css|safe }}<title>{{ title }}</title></head>
<body>
<nav>
  <div class='container topbar'>
    <div class='spacer'></div>
    <a href='/'>Accueil</a>
    <a href='/register'>Créer un compte</a>
    <a href='/login'>Se connecter</a>
  </div>
</nav>
<div class='container' style='margin-top:16px;'>
  <div class='card' style='max-width:540px;margin:auto;'>
    <h2 style='margin-top:0;'>{{ title }}</h2>
    {% with messages = get_flashed_messages() %}
      {% if messages %}
        {% for m in messages %}
          <div class='alert'>{{ m }}</div>
        {% endfor %}
      {% endif %}
    {% endwith %}
    {{ body|safe }}
  </div>
</div>
</body></html>
"""

PPP_HTML = """
<!doctype html><html lang='fr'><head>
<meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'>
<link rel="preload" as="image" href="/static/trade/bg.jpg">
<link rel="icon" href="{{ url_for('static', filename='img/favicon.ico') }}?v=2" type="image/x-icon">
<link rel="icon" type="image/png" sizes="32x32" href="{{ url_for('static', filename='img/favicon-32.png') }}?v=2">
<link rel="icon" type="image/png" sizes="16x16" href="{{ url_for('static', filename='img/favicon-16.png') }}?v=2">
<link rel="shortcut icon" href="{{ url_for('static', filename='img/favicon.ico') }}?v=2">
<link rel="apple-touch-icon" sizes="180x180" href="{{ url_for('static', filename='img/apple-touch-icon.png') }}?v=2">

<meta property="og:title" content="Zeus Meteo">
<meta property="og:description" content="La probabilité de pluie par station, heure par heure, claire et rapide.">
<meta property="og:url" content="https://zeus-meteo.com/">
<meta property="og:type" content="website">
<meta property="og:image" content="{{ url_for('static', filename='img/og-image.png', _external=True) }}">
<meta property="og:image:width" content="1200">
<meta property="og:image:height" content="630">

<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="Zeus Meteo">
<meta name="twitter:description" content="La probabilité de pluie par station, heure par heure, claire et rapide.">
<meta name="twitter:image" content="{{ url_for('static', filename='img/og-image.png', _external=True) }}">
<title>Zeus</title>
{{ css|safe }}
<style>
  .brand { color:#2160f3; font-weight:800; text-decoration:none; text-shadow:none; }
  .brand:hover { color:#64b5f6; }
  .brand.active { color:#79e7ff; text-shadow:0 0 12px rgba(187,134,252,.35); }
  .brand.active:hover { color:#79e7ff; }
  .nav-left .brand + .brand { margin-left:14px; }

  .ppp-day { position:relative; }
  .ppp-day .forecast-wrap {
    position:absolute; top:6px; right:8px; width:22px; height:22px;
    display:flex; align-items:center; justify-content:center; pointer-events:none; z-index:2; opacity:.95;
  }
  .ppp-day .forecast-wrap svg { width:20px; height:20px; }
  .ppp-day .forecast-wrap .icon-drop path { fill:#79e7ff; }
  .ppp-day.disabled .forecast-wrap { opacity:.9; }

  /* Outil éclair */
  .bolt-tool {
    display:inline-flex; align-items:center; justify-content:center;
    width:28px; height:28px; margin-left:16px; font-size:22px; line-height:1;
    cursor:grab; user-select:none; -webkit-user-drag:element;
  }
  .bolt-tool:active { cursor:grabbing; }
  /* badge numérique d'éclairs */
  #boltTool{ position: relative; }
  #boltTool::after{
    content: attr(data-count);
    position: absolute; top: -6px; right: -8px;
    font-size: 11px; font-weight: 800;
    background: #1e88e5; color: #fff;
    border-radius: 999px; padding: 2px 6px;
    box-shadow: 0 2px 8px rgba(0,0,0,.25);
  }

  /* Feedback drop */
  .ppp-day.drop-ok   { outline:2px dashed rgba(255,215,0,.65); outline-offset:3px; }
  .ppp-day.drop-nope { outline:2px dashed rgba(220,20,60,.5); outline-offset:3px; }
  .ppp-day.is-past{
    opacity: .55;
    filter: grayscale(100%);
    pointer-events: none; /* pas cliquable */
  }
  /* Cote boostée */
  .odds.boosted::before { content:"⚡"; margin-right:4px; }

  /* Anti-bogue: même si .disabled global a pointer-events:none */
  .ppp-grid .ppp-day,
  .ppp-grid .ppp-day.disabled { pointer-events:auto; }

  .user-menu { position: relative; display: inline-block; }
  .user-trigger{
    background: transparent; border: 0; color: #fff; font-weight: 800;
    cursor: pointer; display: inline-flex; align-items: center; gap: 6px;
  }
  .user-trigger .caret{ opacity: .8; font-size: 12px; }
  .user-dropdown{
    position: absolute; right: 0; top: 120%;
    background: rgba(13,20,40,.98);
    border: 1px solid rgba(255,255,255,.08);
    border-radius: 12px;
    box-shadow: 0 10px 28px rgba(0,0,0,.35);
    min-width: 180px; padding: 6px; display: none; z-index: 1000;
    backdrop-filter: blur(6px);
  }
  .user-dropdown.open{ display: block; }
  .user-dropdown .item{
    display: block; width: 100%; text-align: left;
    padding: 10px 12px; border-radius: 10px;
    background: transparent; color: #cfe3ff; text-decoration: none;
    border: 0; cursor: pointer; font-weight: 700;
  }
  .user-dropdown .item:hover{ background: rgba(120,180,255,.12); color: #79e7ff; }
  .user-dropdown .item.disabled{
    opacity: .5; cursor: default; pointer-events: none;
  }

  /* Pousse le lien Cabine tout à droite sur PPP */
  .topbar .nav-right { display: flex; align-items: center; }
  .topbar .nav-right a[href^="/🧢"] { margin-left: auto; }

  /* (optionnel) un petit espace fixe entre Carte et le bord avant Cabine */
  .topbar .nav-right .brand-map { margin-right: 1px; }

  /* Fullscreen background image derrière tout */
  body.trade-page::before{
    content:"";
    position:fixed;
    inset:0;
    z-index:-2;
    background:
      linear-gradient(rgba(0,0,0,0.25), rgba(0,0,0,0.25)),
      url("/static/trade/bg.jpg") center / cover no-repeat fixed;
  }
  body.trade-page::after{
  content:"";
  position:fixed;
  inset:0;
  z-index:-1;
  background: radial-gradient(100% 120% at 50% 0%, rgba(7,25,46,.35) 0%, rgba(7,25,46,.75) 100%);
  pointer-events:none;
  }
  .time-row{ margin-top:12px; }
  .time-row label{ display:block; font-size:12px; opacity:.8; margin-bottom:6px; }
</style>
</head><body class="trade-page">
<div class="stars"></div>

<nav>
  <div class="container topbar">
    <div class="nav-left">
      <a href="/ppp" class="topbar-logo-link" aria-label="Rafraîchir la page PPP">
        <img src="{{ url_for('static', filename='img/weather_bets_S.png') }}" alt="Meteo God" class="topbar-logo">
      </a>
    </div>

    <div class="nav-center">
      {% if current_user.is_authenticated %}
        <div class="center-box">
          {% if solde_str %}
            <div class="solde-box">
              <span class="solde-label">Solde&nbsp;:</span>
              <span class="solde-value">{{ solde_str }}</span>
            </div>
          {% endif %}

          <div class="user-menu">
            <button class="user-trigger" id="userMenuBtn" aria-haspopup="true" aria-expanded="false">
              <strong>{{ current_user.username }}</strong>
              <span class="caret">▾</span>
            </button>
            <div class="user-dropdown" id="userDropdown" role="menu">
              <a class="item" href="{{ url_for('trade_page') }}">Échanges 🤝</a>
              <a class="item" href="/static/dessin/dessin.html">Offrandes 🎨</a>
              <a class="item" href="{{ url_for('cabine_page') }}">Profil 👔</a>            
              <a class="item" href="/carte">Carte 🗺️</a>
              <a class="item" href="{{ url_for('wet') }}">Humidité 💧</a>
              <div class="submenu">
                <button class="item submenu-toggle" id="optionsBtn" type="button">Options ▸</button>
                <div class="submenu-panel" id="optionsMenu" hidden>
                  <form id="deleteAccountForm" action="{{ url_for('delete_account') }}" method="POST"
                        onsubmit="return confirm('Supprimer définitivement ce compte ? Cette action est irréversible.');">
                    <button type="submit" class="danger">Supprimer ce compte</button>
                  </form>
                </div>
              </div>       
              <a class="item" href="/logout">Se déconnecter</a>
            </div>
          </div>
        </div>
      {% else %}
        <div class="center-box">
          <a href="/register">Créer un compte</a>
          <a href="/login">Se connecter</a>
        </div>
      {% endif %}
    </div>
    <div class="nav-right">
      <span
        id="boltTool"
        class="bolt-tool"
        draggable="true"
        data-count="{{ current_user.bolts or 0 }}"
        title="Éclairs restants : {{ current_user.bolts or 0 }}"
      >⚡</span>
      <a id="trade-unread"
         class="badge-unread"
         href="{{ url_for('trade_page') }}"
         aria-label="Aller au marché (Trade)"
         style="display:none; margin-left:.5rem;">
        NOUVEAU MESSAGE
      </a>
    </div>
  </div>
</nav>

<div class="container" style="margin-top:16px;">
  <div class="card">
    <h2>{{ city_label }}</h2>
    <p class="muted"></p>
    <div id="pppGrid" class="ppp-grid"></div>
  </div>
</div>

<!-- modal -->
<div id="pppModal" class="ppp-modal">
  <div class="ppp-card">
    <h3 style="margin:0 0 8px;" id="mTitle"></h3>

    <div id="pppHistory" style="margin:8px 0; font-size:14px; color:#a8b0c2;"></div>

    <p id="mOddsWrap" style="margin:0 0 8px;">
      <strong>Cote:</strong> x<span id="mOdds"></span>
    </p>

    <div id="mHistory"
         class="m-history"
         style="margin-bottom:10px; font-size:14px; color:#ccc; display:none;">
    </div>

    <form method="post"
          action="{{ url_for('ppp', station_id=station_id) if station_id else url_for('ppp') }}"
          id="pppForm">

      <!-- valeurs cachées -->
      <input type="hidden" name="date" id="mDateInput">
      <input type="hidden" name="target_dt" id="mTargetDt">
      {% if station_id is not none %}
      <input type="hidden" name="station_id" value="{{ station_id }}">
      {% endif %}

      <div class="grid cols-3"
           style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px;">

        <!-- Choix pluie / pas pluie -->
        <div>
          <label for="mChoice">Choix</label>
          <select name="choice" id="mChoice" required>
            <option value="PLUIE">💧 Pluie</option>
            <option value="PAS_PLUIE">☀️ Pas Pluie</option>
          </select>
        </div>

        <!-- Choix de l’heure -->
        <div>
          <label for="mHour">Heure</label>
          <select name="target_time" id="mHour" required>
            {% for h in range(0, 24) %}
            <option value="{{ "%02d:00" % h }}" {% if h == 15 %}selected{% endif %}>
              {{ "%02d" % h }}h
            </option>
            {% endfor %}
          </select>
        </div>

        <!-- Montant -->
        <div>
          <label for="mAmount">Montant (points)</label>
          <input type="number"
                 name="amount"
                 id="mAmount"
                 min="0"
                 step="0.1"
                 value="1.0"
                 required>
        </div>
      </div>

      <div style="margin-top:12px;display:flex;gap:8px;justify-content:flex-end;">
        <button type="button" class="btn" id="mCancel">Annuler</button>
        <button class="btn primary">Miser</button>
      </div>
    </form>
  </div>
</div>

<script>
(function(){
  function fmtPts(x){
    // arrondi à 1 décimale, puis supprime la décimale inutile (,0)
    const v = Math.round((Number(x) || 0) * 10) / 10;
    let s = v.toFixed(1).replace('.', ',');
    return s.replace(/,0$/, '');  // 2,0 -> 2
  }
  // --- Aujourd'hui (Europe/Paris) ---
  const now = new Date();
  const parisNow = new Date(now.toLocaleString('en-US', { timeZone: 'Europe/Paris' }));
  const today = new Date(parisNow.getFullYear(), parisNow.getMonth(), parisNow.getDate());

  // Cotes de base côté client
  const ODDS = {
    0:null,1:1.0,2:1.0,3:1.1,4:1.2,5:1.3,6:1.4,7:1.5,8:1.6,9:1.7,10:1.8,
    11:2.0,12:2.0,13:2.0,14:2.0,15:2.0,16:2.0,17:2.0,18:2.0,19:2.5,20:2.5,
    21:2.4,22:2.3,23:2.2,24:2.2,25:2.0,26:2.1,27:2.4,28:2.7,29:2.8,30:2.9,31:3.0
  };

  // Refs DOM
  const grid       = document.getElementById('pppGrid');
  const modal      = document.getElementById('pppModal');
  const mOddsEl    = document.getElementById('mOdds');
  const mDateInput = document.getElementById('mDateInput');
  const mCancel    = document.getElementById('mCancel');
  const form       = document.getElementById('pppForm');
  const mTimeHidden = document.getElementById('mTargetDt');

  if (!grid) { console.error('[ppp] #pppGrid introuvable'); return; }

  // Données serveur
  const MY_BETS = {{ bets_map  | default({}) | tojson | safe }};
  const BOOSTS  = {{ boosts_map| default({}) | tojson | safe }};

  // Ville cible accessible partout
  const qCity = {{ city_label | tojson }};

  console.debug('[ppp] BOOSTS from server:', BOOSTS);

  // Utils
  function ymd(d){
    const y = d.getFullYear();
    const m = String(d.getMonth()+1).padStart(2,'0');
    const day = String(d.getDate()).padStart(2,'0');
    return `${y}-${m}-${day}`;
  }
  function fr(d){
    return d.toLocaleDateString('fr-FR', { weekday:'short', day:'2-digit', month:'short' });
  }
  function hasBetFor(key){
    const b = MY_BETS && MY_BETS[key];
    if (!b) return false;
    return (Array.isArray(b.bets) && b.bets.length > 0) || (typeof b.amount === 'number' && b.amount > 0);
  }

  // Icônes
  const svgDrop = `<svg viewBox="0 0 24 24" class="stake-icon icon-drop" aria-hidden="true"><path d="M12 2 C12 2, 6 8, 6 12 a6 6 0 0 0 12 0 C18 8, 12 2, 12 2z"></path></svg>`;
  const svgSun  = "☀️";

  // Rendu des cotes
  function renderOdds(oddsEl, baseOdds, boostVal){
    if (!oddsEl) return;
    const base  = Number.isFinite(Number(baseOdds)) ? Number(baseOdds) : 0;
    const boost = Number.isFinite(Number(boostVal)) ? Number(boostVal) : 0;
    const val   = base + boost;
    oddsEl.textContent = val > 0 ? ('x' + val.toString().replace('.', ',')) : '';
    oddsEl.classList.toggle('boosted', boost > 0);
  }

  // Submit guard
  if (form) {
    form.addEventListener('submit', function (e) {
      const hasDate = !!(mDateInput && mDateInput.value);
      if (!hasDate) {
        e.preventDefault();
        alert("Cliquez d'abord sur un jour du calendrier pour choisir la date.");
        return;
      }

      // heure choisie (menu déroulant)
      const hourSel = document.getElementById('mHour');
      const hhmm = (hourSel && hourSel.value) ? hourSel.value : '18:00';

      // alimente le champ caché "target_dt" pour le backend (optionnel si tu lis déjà target_time)
      if (mTimeHidden) {
        mTimeHidden.value = `${mDateInput.value}T${hhmm}`; // ex: 2025-11-15T18:00
      }
      // Le select <select name="target_time" id="mHour"> soumettra aussi target_time=HH:MM.
    });
  }

  // Normalisation ODDS/BOOSTS
  const ODDS_SAFE = Array.from({ length: 31 }, (_, i) => {
    const v = (ODDS && Object.prototype.hasOwnProperty.call(ODDS, i)) ? Number(ODDS[i]) : NaN;
    return Number.isFinite(v) && v > 0 ? v : 1;
  });
  const BOOSTS_SAFE = (BOOSTS && typeof BOOSTS === 'object') ? BOOSTS : {};

  // --- Grille de 31 jours (démarre à J-3) ---
  const START_SHIFT = -3; // ← on commence 3 jours avant aujourd’hui
  const TOTAL_DAYS  = 34;

  for (let i = 0; i <= TOTAL_DAYS; i++) {
    const delta = i + START_SHIFT;         // -3 … +31 vs today (34 inclus → +31)
    const d     = addDaysLocal(today, delta);
    const key   = ymdParis(d);

    const el = document.createElement('div');
    el.className = 'ppp-day' + (delta === 0 ? ' today' : '');
    el.setAttribute('data-key', key);
    el.setAttribute('data-idx', String(delta)); // idx relatif à aujourd’hui (peut être négatif)

    const betInfo = (MY_BETS && MY_BETS[key]) ? MY_BETS[key] : null;
    const amount  = betInfo ? (Number(betInfo.amount) || 0) : 0;
    const choice  = betInfo ? betInfo.choice : null;

    // --- Jours passés : grisés si aucun pari, sinon verdict visible (priorité rouge) ---
    if (delta < 0) {
      if (!hasBetFor(key)) {
        el.classList.add('is-past');
      } else {
        if (betInfo && Array.isArray(betInfo.bets) && betInfo.bets.length > 0) {
          const results = betInfo.bets
            .map(b => (b.result || b.verdict || '').toUpperCase())
            .filter(Boolean);
          if (results.includes('LOSE')) {
            el.classList.add('today-loss');   // ❌ priorité rouge
          } else if (results.includes('WIN')) {
            el.classList.add('today-win');    // ✅ sinon vert
          }
        } else {
          const result = (betInfo?.result || betInfo?.verdict || '').toUpperCase();
          if (result === 'LOSE') el.classList.add('today-loss');
          else if (result === 'WIN') el.classList.add('today-win');
        }
      }
    }

    // Interdiction de mise : J+0..J+3 si aucune mise
    if (delta <= 3 && delta >= 0 && !hasBetFor(key)) {
      el.classList.add('disabled');
    }

    let stakeBlock = '';
    if (amount > 0) {
      const icon = (choice === 'PLUIE') ? svgDrop : (choice === 'PAS_PLUIE') ? svgSun : '';
      stakeBlock = `
        <div class="stake-wrap">
          ${icon}
          <div class="stake-amt">+${fmtPts(amount)}</div>
        </div>`;
    }

    el.innerHTML = `
      <div class="date">${fr(d)}</div>
      ${stakeBlock}
      <div class="odds"></div>
    `;

    const oddsEl   = el.querySelector('.odds');
    // Les cotes client sont définies pour des deltas >= 0 → clamp sur [0..30]
    const baseIdx  = Math.max(0, Math.min(30, delta));
    const baseOdds = ODDS_SAFE[baseIdx];
    const boostForDay = Number.isFinite(Number(BOOSTS_SAFE[key])) ? Number(BOOSTS_SAFE[key]) : 0;
    renderOdds(oddsEl, baseOdds, boostForDay);

    // Clic → modal (jours passés consultables, futur misable selon règles)
    el.addEventListener('click', () => {
      const hasBetNow = hasBetFor(key);
      const isPast = (delta < 0);

      const titleEl   = document.getElementById('mTitle');
      const oddsWrap  = document.getElementById('mOddsWrap');
      const histWrap  = document.getElementById('mHistory');

      // Titre
      if (titleEl) {
        if (isPast || (delta <= 3 && hasBetNow)) {
          titleEl.textContent = fr(d);
        } else {
          titleEl.textContent = "Miser sur " + fr(d);
        }
      }

      // Cote (utile seulement si le formulaire est visible)
      let shownOdds = baseOdds + (BOOSTS_SAFE[key] || 0);
      const txt = (oddsEl.textContent || '').trim();
      if (txt) {
        const num = parseFloat(txt.replace(/^x/i,'').replace(',','.'));
        if (!isNaN(num)) shownOdds = num;
      }

      // Historique (lecture seule + verdict/pluie)
      if (histWrap) {
        histWrap.innerHTML = '';
        if (hasBetNow) {
          const list = (betInfo && Array.isArray(betInfo.bets)) ? betInfo.bets : [];
          const totalAmount = Math.round(list.reduce((acc, b) => acc + (Number(b.amount) || 0), 0) * 100) / 100;

          let weightedSum = 0;
          for (const b of list) {
            const a = Number(b.amount) || 0;
            const o = Number(b.odds);
            const odd0 = (Number.isFinite(o) && o > 0) ? o : baseOdds;
            weightedSum += a * odd0;
          }

          const initialOdds = (totalAmount > 0 && Number.isFinite(weightedSum / totalAmount))
            ? (weightedSum / totalAmount)
            : baseOdds;

          const boostTotal = Number(BOOSTS_SAFE[key] || 0);
          const boltCount  = Math.round(boostTotal / 5);
          const potentialWithBoosts = weightedSum + boostTotal * totalAmount;

          const lines = [];
          for (const b of list) {
            const whenDate = new Date(b.when);
            const frWhen = whenDate.toLocaleDateString('fr-FR', {
              weekday:'short', day:'2-digit', month:'short', year:'numeric'
            });

            const amt = fmtPts(b.amount);
            const o   = Number(b.odds);
            const odd = (Number.isFinite(o) && o > 0 ? o : initialOdds)
                          .toFixed(1).replace('.', ',');

            // heure cible : fallback 18:00 si non fournie
            const rawTime = String(b.target_time || b.time || '18:00');
            const hhmm = rawTime.slice(0,5);
            const hh   = hhmm.split(':')[0] || '18';

            // verdict icône
            const verdict = String(b.verdict || b.result || '').toUpperCase();
            const badge   = verdict === 'WIN' ? ' ✅' : verdict === 'LOSE' ? ' ❌' : '';

            // pluie mesurée (mm) si dispo
            let observedNote = '';
            if (b.observed_mm != null && b.observed_mm !== '') {
              const mm = Number(b.observed_mm);
              if (!Number.isNaN(mm)) {
                observedNote = ` (${mm.toFixed(1).replace('.', ',')} mm observés à ${hh}h)`;
              }
            }

            lines.push(`Mise du ${frWhen} — ${hh}h : ${amt} pts — (x${odd})${badge}${observedNote}`);
          }

          if (boltCount > 0) lines.push(`Éclairs : ${boltCount} — (x5)`);
          lines.push(`Gains potentiels : ${potentialWithBoosts.toFixed(2).replace('.', ',')} pts`);

          histWrap.innerHTML = lines.map(l => `<div>${l}</div>`).join('');
          histWrap.style.display = 'block';
        } else {
          histWrap.innerHTML = `<div>Aucune mise pour ce jour.</div>`;
          histWrap.style.display = 'block';
        }
      }

      // Affichage du formulaire : masqué pour jours passés, masqué pour J+0..J+3 (s'il n'y a pas déjà une mise)
      const showForm = !isPast && !(delta <= 3 && !hasBetNow);

      if (form) form.style.display = showForm ? 'block' : 'none';
      if (oddsWrap) oddsWrap.style.display = showForm ? 'block' : 'none';
      if (showForm && mOddsEl) {
        mOddsEl.textContent = shownOdds.toFixed(1).replace('.', ',');
      }

      // Préremplir date & heure si on peut miser
      if (showForm) {
        if (mDateInput) mDateInput.value = key;

        const hourSel = document.getElementById('mHour');
        if (hourSel) {
          const existing = (betInfo && (betInfo.target_time || (betInfo.bets?.[0]?.target_time))) || '';
          hourSel.value = existing || '18:00';
        }
        if (mTimeHidden) mTimeHidden.value = ''; // nettoie le champ caché
      }

      if (modal) modal.classList.add('open');
    });

    grid.appendChild(el);
  }

  // Nettoyage cotes
  document.querySelectorAll('.ppp-day .odds').forEach(o => {
    if (!o.textContent || !o.textContent.trim()) return;
    o.textContent = o.textContent.replace(/^[⚡\\s]+/g, '').replace(/^x?/, 'x');
  });

  // Icônes météo
  function ensureForecastWrap(cell){
    if (!cell) return null;
    let wrap = cell.querySelector('.forecast-wrap');
    if (!wrap) {
      wrap = document.createElement('div');
      wrap.className = 'forecast-wrap';
      cell.prepend(wrap);
    }
    return wrap;
  }
  function addDaysLocal(d, n){
    const x = new Date(d.getTime());
    x.setDate(x.getDate() + n); // gère DST correctement
    return x;
  }
  function ymdParis(d){
    // fabrique la clé YYYY-MM-DD en “Europe/Paris”
    const y = d.toLocaleString('en-CA', { timeZone: 'Europe/Paris', year:'numeric' });
    const m = d.toLocaleString('en-CA', { timeZone: 'Europe/Paris', month:'2-digit' });
    const day = d.toLocaleString('en-CA', { timeZone: 'Europe/Paris', day:'2-digit' });
    return `${y}-${m}-${day}`;
  }
  function frParis(d){
    return d.toLocaleDateString('fr-FR', {
      timeZone:'Europe/Paris',
      weekday:'short', day:'2-digit', month:'short'
    });
  }
  function clampTimeToHour(hhmm){
    // Normalise "18" -> "18:00", "18:7" -> "18:07"
    const s = String(hhmm || '').trim();
    if (!s) return '18:00';
    const parts = s.split(':');
    const h = Math.max(0, Math.min(23, parseInt(parts[0]||'0',10)));
    const m = Math.max(0, Math.min(59, parseInt(parts[1]||'0',10)));
    return String(h).padStart(2,'0') + ':' + String(m).padStart(2,'0');
  }

  (function loadTodayIcon(){
    const todayKey = ymd(today);
    const cell = document.querySelector(`.ppp-day[data-key="${todayKey}"]`);
    if (!cell) return;

    const wrap = ensureForecastWrap(cell);

    fetch('/api/meteo/today?city=' + encodeURIComponent(qCity))
      .then(r => r.ok ? r.json() : null)
      .then(data => {
        if (!data) return;

        // Détermine pluie/pas pluie (même logique que chez toi)
        let isRain = false;
        if (data.pop != null) {
          let pop = Number(data.pop); if (pop > 1) pop /= 100;
          isRain = pop >= 0.45;
        } else {
          isRain = (Number(data.rain_hours) >= 4) || (Number(data.code) >= 60);
        }

        // Icône
        wrap.innerHTML = isRain ? svgDrop : svgSun;

        // Pari du jour ?
        const betInfo = (MY_BETS && MY_BETS[todayKey]) ? MY_BETS[todayKey] : null;
        const hasBet  = (typeof hasBetFor === 'function') ? hasBetFor(todayKey) : !!betInfo;

        // Couleur du liseré (win/lose) — logique "du jour" (conserve ton existant)
        cell.classList.remove('today-win','today-loss');
        if (hasBet) {
          const choice = betInfo ? betInfo.choice : null; // 'PLUIE' ou 'PAS_PLUIE'
          if (choice === 'PLUIE' || choice === 'PAS_PLUIE') {
            const isWin = (choice === 'PLUIE' && isRain) || (choice === 'PAS_PLUIE' && !isRain);
            cell.classList.add(isWin ? 'today-win' : 'today-loss');
          }
        }
      })
      .catch(()=>{});
  })();

  (async function loadForecastIcons(){
    try {
      const r5 = await fetch('/api/meteo/forecast5?city=' + encodeURIComponent(qCity));
      if (!r5.ok) return;
      const data = await r5.json();
      if (!data || !Array.isArray(data.forecast5)) return;
      const limitEnd = new Date(today.getTime() + 13*24*3600*1000);
      for (const f of data.forecast5) {
        const dt = new Date(f.date + 'T00:00:00');
        if (dt < today || dt > limitEnd) continue;
        const cell = document.querySelector(`.ppp-day[data-key="${f.date}"]`);
        if (!cell) continue;
        const wrap = ensureForecastWrap(cell);
        let isRain = false;
        if (f.pop != null) {
          let pop = +f.pop; if (pop > 1) pop /= 100;
          isRain = pop >= 0.45;
        } else {
          isRain = (f.rain_hours >= 4) || (f.code >= 60);
        }
        wrap.innerHTML = isRain ? svgDrop : svgSun;
      }
    } catch(e){
      console.error('[ppp] forecast icons error:', e);
    }
  })();

  // Fermer la modale
  if (mCancel) {
    mCancel.addEventListener('click', function () {
      if (modal) modal.classList.remove('open');
    });
  }
  if (modal) {
    modal.addEventListener('click', function (e) {
      if (e.target === modal) modal.classList.remove('open');
    });
  }
  // --- Gestion du stock d'éclairs ---
  function setBoltCount(n){
    const bolt = document.getElementById('boltTool');
    if (!bolt) return;
    const count = Math.max(0, Number(n||0));
    bolt.dataset.count = String(count);
    bolt.title = count > 0 ? `Éclairs restants : ${count}` : `Plus d’éclairs`;
    bolt.style.opacity = (count > 0) ? '1' : '.35';
    bolt.style.pointerEvents = (count > 0) ? 'auto' : 'none';
  }
  async function fetchBoltCount(){
    try{
      const r = await fetch('/api/users/bolts', { credentials:'same-origin' });
      if (!r.ok) return;
      const j = await r.json();
      setBoltCount(j.bolts);
    }catch(_){}
  }

  // --- Drag source de l’éclair (unique) ---
  const bolt = document.getElementById('boltTool');
  if (bolt){
    fetchBoltCount();
    bolt.setAttribute('draggable','true');
    bolt.style.webkitUserDrag = 'element';
    bolt.addEventListener('dragstart', (ev) => {
      const count = Number(bolt.dataset.count || '0');
      if (count <= 0) {
        ev.preventDefault();
        return;
      }
      try {
        ev.dataTransfer.setData('text/plain', 'bolt');
        ev.dataTransfer.effectAllowed = 'copy';
      } catch (e) { /* no-op */ }
    });    
  }

  // --- Utilitaire: cible -> cellule robuste ---
  function cellFromEvent(ev){
    let t = ev.target;
    if (t && t.nodeType === 3) t = t.parentElement; // Text -> Element
    if (!(t instanceof Element)) return null;
    const cell = t.closest('.ppp-day');
    return (cell && grid.contains(cell)) ? cell : null;
  }

  // --- DnD délégué (unique) ---
  grid.addEventListener('dragenter', (ev) => {
    const cell = cellFromEvent(ev);
    if (!cell) return;
    const ok = hasBetFor(cell.dataset.key);
    cell.classList.add('drop-candidate');
    cell.classList.toggle('drop-ok', ok);
    cell.classList.toggle('drop-nope', !ok);
  });

  grid.addEventListener('dragover', (ev) => {
    const cell = cellFromEvent(ev);
    if (!cell) return;
    ev.preventDefault(); // indispensable
    const ok = hasBetFor(cell.dataset.key);
    try { ev.dataTransfer.dropEffect = ok ? 'copy' : 'none'; } catch (_) {}
  });

  grid.addEventListener('dragleave', (ev) => {
    const cell = cellFromEvent(ev);
    if (!cell) return;
    cell.classList.remove('drop-candidate','drop-ok','drop-nope');
  });

  grid.addEventListener('drop', async (ev) => {
    const cell = cellFromEvent(ev);
    if (!cell) return;
    ev.preventDefault();
    cell.classList.remove('drop-candidate','drop-ok','drop-nope');

    const key = cell.dataset.key;
    const idx = Number(cell.dataset.idx);

    if (!hasBetFor(key)) {
      cell.classList.add('shake');
      setTimeout(() => cell.classList.remove('shake'), 500);
      return;
    }

    const payload = (ev.dataTransfer && ev.dataTransfer.getData('text/plain')) || 'bolt';
    if (payload !== 'bolt') return;

    const oddsEl   = cell.querySelector('.odds');
    const baseOdds = ODDS_SAFE[Math.max(0, Math.min(30, idx))];

    try {
      const resp = await fetch('/ppp/boost', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          date: key,
          value: 5.0,
          station_id: {{ station_id | default(None) | tojson }}
        })
      });

      // Même si le serveur ne met pas ok:true, on essaie quand même d'extraire une valeur
      let total = 0;
      try {
        const json = await resp.json();
        // Met à jour le stock d'éclairs si fourni par le backend
        if (json && typeof json.bolts_left !== 'undefined') {
          setBoltCount(json.bolts_left);
        }
        // Liste de clés possibles renvoyées par l'API
        const candidates = [
          'total','new_total','boost_total','total_boost','boost','value','newTotal','cumul'
        ];
        for (const k of candidates) {
          if (json && json[k] != null) {
            const v = (typeof json[k] === 'string') ? parseFloat(String(json[k]).replace(',', '.')) : Number(json[k]);
            if (!Number.isNaN(v)) { total = v; break; }
          }
        }
        console.debug('[ppp] boost resp:', json, '→ total=', total);
      } catch (e) {
        console.warn('[ppp] boost: réponse non-JSON ou vide, fallback +5', e);
      }

      // Filet de sécurité : si on n'a rien pu lire, on incrémente localement de +5
      if (!Number.isFinite(total) || total <= 0) {
        const prev = Number(BOOSTS_SAFE[key] || 0);
        total = prev + 5;
      }

      BOOSTS_SAFE[key] = total;           // MAJ locale
      renderOdds(oddsEl, baseOdds, total);

    } catch(e){
      console.error('[ppp] boost error:', e);
    }
  });
})();

/* ---------- Menu utilisateur (topbar) ---------- */
(function(){
  const btn = document.getElementById('userMenuBtn');
  const dd  = document.getElementById('userDropdown');
  if (!btn || !dd) return;

  function closeMenu(){
    dd.classList.remove('open');
    btn.setAttribute('aria-expanded','false');
    // ferme aussi le sous-menu Options s'il est ouvert
    const optionsMenu = document.getElementById('optionsMenu');
    if (optionsMenu) optionsMenu.setAttribute('hidden','');
  }

  function toggleMenu(){
    const isOpen = dd.classList.toggle('open');
    btn.setAttribute('aria-expanded', isOpen ? 'true' : 'false');
    if (!isOpen) {
      const optionsMenu = document.getElementById('optionsMenu');
      if (optionsMenu) optionsMenu.setAttribute('hidden','');
    }
  }

  btn.addEventListener('click', (e) => {
    e.stopPropagation();
    toggleMenu();
  });

  document.addEventListener('click', () => closeMenu());

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') closeMenu();
  });

  // --- Gestion du sous-menu Options ---
  const optionsBtn  = document.getElementById('optionsBtn');
  const optionsMenu = document.getElementById('optionsMenu');
  if (optionsBtn && optionsMenu){
    optionsBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      const isHidden = optionsMenu.hasAttribute('hidden');
      optionsMenu.toggleAttribute('hidden', !isHidden);
    });

    // clic hors du dropdown → ferme aussi le sous-menu
    document.addEventListener('click', (e) => {
      if (!dd.contains(e.target)) {
        optionsMenu.setAttribute('hidden', '');
      }
    });
  }

  // --- Badge “nouveau message” (PPP) basé sur /api/chat/unread-summary ---
  async function refreshPPPUnread() {
    try {
      const r = await fetch('/api/chat/unread-summary', { credentials: 'same-origin' });
      if (!r.ok) throw 0;
      const arr = await r.json();  // ex: [{from_user_id: 12, count: 3}, ...]
      const total = Array.isArray(arr) ? arr.reduce((s,x)=> s + (Number(x.count)||0), 0) : 0;

      const badge = document.getElementById('trade-unread');
      if (!badge) return;
      badge.style.display = total > 0 ? 'inline-block' : 'none';
    } catch(e) {
      /* silencieux */
    }
  }

  document.addEventListener('DOMContentLoaded', () => {
    refreshPPPUnread();
    setInterval(refreshPPPUnread, 20000);
  });

  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') refreshPPPUnread();
  });
})();
</script>
</body></html>
"""

WET_HTML = """

<!doctype html><html lang='fr'><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Wet — Humidité (48h)</title>
{{ css|safe }}
<style>
/* --- Topbar brand styles --- */
.topbar .brand{
  color:#2160f3;
  font-weight:800;
  text-decoration:none;
  text-shadow:none;
}
.topbar .brand:hover{ color:#64b5f6; }

.topbar .brand.active{
  color:#79e7ff !important;
  text-shadow:0 0 12px rgba(187,134,252,.35);
}
.topbar .brand.active:hover{ color:#64b5f6 !important; }

.nav-left .brand + .brand { margin-left:14px; }

.topbar-logo {
  height:22px; margin-left:16px;
  display:inline-block; vertical-align:middle;
}

.wet-grid{ display:grid; grid-template-columns:1fr; gap:10px; }
@media (min-width:900px){ .wet-grid{ grid-template-columns:1fr 1fr; } }
.wet-daycard{ background:var(--card-bg); border:1px solid var(--card-border); border-radius:16px; padding:14px; box-shadow:0 10px 30px rgba(0,0,0,.25); }
.wet-daytitle{ margin:0 0 10px; text-transform:capitalize; color:var(--text); font-size:16px; }
.wet-innergrid{ display:grid; grid-template-columns:repeat(2, minmax(0,1fr)); gap:10px; }
@media (min-width:700px){ .wet-innergrid{ grid-template-columns:repeat(3, minmax(0,1fr)); } }
@media (min-width:1000px){ .wet-innergrid{ grid-template-columns:repeat(4, minmax(0,1fr)); } }

.wet-cell{
  position:relative;
  background: rgba(255,255,255,.03);
  border:1px solid var(--card-border);
  border-radius:12px;
  padding:28px 14px;
  min-height:70px;
  transition: border-color .2s ease, transform .15s ease, background-color .2s ease, opacity .2s ease;
}
.wet-cell:hover{ transform: translateY(-2px); border-color: rgba(121,231,255,.25); }

/* Heure en haut-gauche */
.wet-time{
  position:absolute; top:8px; left:10px; font-weight:600;
}

/* CIBLE (humidité misée) — maintenant au CENTRE-GAUCHE */
.wet-target{
  position:absolute;
  left:10px;
  top:50%;
  transform: translateY(-50%);
  font-weight:700;
  opacity:.95;
  color:#79e7ff;
  text-shadow:0 0 8px rgba(121,231,255,.25);
}

/* Cote éventuelle en bas-droite (si utilisée) */
.wet-odds{
  position:absolute; right:10px; bottom:8px;
  font-weight:700; text-shadow:0 0 12px rgba(187,134,252,.35);
}

/* Mise en bas-gauche (inchangé) */
.wet-stake{
  position:absolute; left:12px; bottom:8px;
  color:#7ef7c0; font-weight:700;
}

/* HUMIDITÉ OBSERVÉE — centre-droite */
.wet-cell .obs-rh{
  position:absolute; right:6px; top:50%;
  transform: translateY(-50%);
  font-size:.9em; color: var(--muted);
  background: rgba(0,0,0,0.3);
  padding:1px 4px; border-radius:4px; z-index:1;
}

/* Édition verrouillée */
.input-readonly{ opacity:.8; cursor:not-allowed; }

/* Cases grisées (passé) — ta classe existante */
.wet-cell.disabled{
  opacity:.35;
  pointer-events:none;
  filter:grayscale(.4);
}

/* Menu utilisateur (inchangé) */
.user-menu { position: relative; display: inline-block; }
.user-trigger{
  background: transparent; border: 0; color: #fff; font-weight: 800;
  cursor: pointer; display: inline-flex; align-items: center; gap: 6px;
}
.user-trigger .caret{ opacity: .8; font-size: 12px; }
.user-dropdown{
  position: absolute; right: 0; top: 120%;
  background: rgba(13,20,40,.98);
  border: 1px solid rgba(255,255,255,.08);
  border-radius: 12px;
  box-shadow: 0 10px 28px rgba(0,0,0,.35);
  min-width: 180px; padding: 6px; display: none; z-index: 1000;
  backdrop-filter: blur(6px);
}
.user-dropdown.open{ display: block; }
.user-dropdown .item{
  display: block; width: 100%; text-align: left;
  padding: 10px 12px; border-radius: 10px;
  background: transparent; color: #cfe3ff; text-decoration: none;
  border: 0; cursor: pointer; font-weight: 700;
}
.user-dropdown .item:hover{ background: rgba(120,180,255,.12); color: #79e7ff; }
.user-dropdown .item.disabled{
  opacity: .5; cursor: default; pointer-events: none;
}

.wet-cell .obs-rh.fade-in{ opacity:0; animation: fadeIn .4s forwards; }
@keyframes fadeIn { from{opacity:0; transform:scale(0.9);} to{opacity:1; transform:scale(1);} }

.wet-grid-wrap{ position: relative; }
#wet-current-arrow{
  position:absolute; top:-10px; width:0; height:0;
  border-left:8px solid transparent; border-right:8px solid transparent;
  border-top:10px solid #16a34a; /* green */
  display:none; z-index:5;
}

</style>

</head><body>
<div class="stars"></div>

<nav>
  <div class="container topbar">
    <div class="nav-left">
      <a href="/ppp" class="topbar-logo-link" aria-label="Rafraîchir la page PPP">
        <img src="{{ url_for('static', filename='img/weather_bets_S.png') }}" alt="Meteo God" class="topbar-logo">
      </a>
    </div>
    <div class="nav-center">
      {% if current_user.is_authenticated and solde_str %}
        <div class="solde-box"><span class="solde-label">Solde&nbsp;:</span><span class="solde-value">{{ solde_str }}</span></div>
      {% endif %}
    </div>
    <div class="nav-right">
      {% if current_user.is_authenticated %}
        <div class="user-menu">
          <button class="user-trigger" id="userMenuBtn" aria-haspopup="true" aria-expanded="false">
            <strong>{{ current_user.username }}</strong>
            <span class="caret">▾</span>
          </button>
          <div class="user-dropdown" id="userDropdown" role="menu">
            <button class="item disabled" type="button" aria-disabled="true" title="Bientôt">Profil</button>
            <a class="item" href="/logout">Se déconnecter</a>
          </div>
        </div>
      {% else %}
        <a href="/register">Créer un compte</a>
        <a href="/login">Se connecter</a>
      {% endif %}
      <a class="nav-link {{ 'active' if request.path.startswith('/cabine') else '' }}"
         href="{{ url_for('cabine_page') }}"></a>
    </div>
  </div>
</nav>

<div class="container" style="margin-top:16px;">
  <div class="card">
    <h2>Wet — Miser sur l’humidité (prochaines 48h)</h2>
    <p class="muted">Choisissez une heure parmi les 48 prochaines. Vous pariez sur un taux d’humidité cible. Gagnez si l’humidité <strong>∈ [cible−3%, cible+3%]</strong>. Si l’humidité est <strong>exactement</strong> égale à la cible, le gain est doublé.</p>
    <div class="wet-grid-wrap"><div id="wet-current-arrow" aria-hidden="true"></div><div id="wetGrid" class="wet-grid"></div></div>
  </div>
</div>

<div id="wetModal" class="ppp-modal">
  <div class="ppp-card">
    <h3 id="wTitle" style="margin:0 0 8px;"></h3>
    <div id="wExisting" class="muted" style="margin:6px 0; display:none;"></div>
    <p id="wOddsWrap" style="margin:0 0 8px;"><strong>Cote:</strong> x<span id="wOdds"></span></p>
    <form method="post" action="/wet" id="wetForm">
      <input type="hidden" name="slot" id="wSlot">
      <div class="grid cols-3" style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px;">
        <div><label>Taux cible (%)</label><input type="number" name="target" id="wTarget" min="0" max="100" step="1" value="80" required></div>
        <div><label>Montant (pts)</label><input type="number" name="amount" id="wAmount" min="0" step="0.1" value="1.0" required></div>
        <div style="align-self:end;"><button class="btn primary" type="submit">Miser</button></div>
      </div>
      <div class="muted" style="margin-top:8px;">Fenêtre de gain : ±3 points. Exactement égal → gain doublé.</div>
    </form>
    <div style="margin-top:12px;display:flex;justify-content:flex-end;">
      <button type="button" class="btn" id="wCancel">Fermer</button>
    </div>
  </div>
</div>


<script>
const stationId = "{{ current_station_id }}";
const date = "{{ today_str }}";

function parisCurrentHourISO() {
  const d = new Date(new Date().toLocaleString("en-US", { timeZone: "Europe/Paris" }));
  d.setMinutes(0,0,0);
  const y = d.getFullYear(), m = String(d.getMonth()+1).padStart(2,'0'), da = String(d.getDate()).padStart(2,'0'), h = String(d.getHours()).padStart(2,'0');
  return `${y}-${m}-${da}T${h}:00:00`;
}

(function(){
  const grid   = document.getElementById('wetGrid');
  const modal  = document.getElementById('wetModal');
  const wTitle = document.getElementById('wTitle');
  const wOdds  = document.getElementById('wOdds');
  const wSlot  = document.getElementById('wSlot');
  const wTarget= document.getElementById('wTarget');
  const wCancel= document.getElementById('wCancel');

  function openWetModal(){
    const m = document.getElementById('wetModal');
    if(!m) return;
    m.style.display = 'block';
    m.classList.add('open');
    m.setAttribute('aria-hidden','false');
  }
  function closeWetModal(){
    const m = document.getElementById('wetModal');
    if(!m) return;
    m.classList.remove('open');
    m.setAttribute('aria-hidden','true');
    m.style.display = 'none';
  }
  function placeCurrentArrow(){
    try{
      const wrap = document.querySelector('.wet-grid-wrap');
      const arrow = document.getElementById('wet-current-arrow');
      if(!wrap || !arrow){ return; }
      const nowCell = document.querySelector('.wet-cell.is-now') 
                   || document.querySelector('.wet-cell[data-current="1"]');
      const cells = Array.from(document.querySelectorAll('.wet-cell'));
      const anchor = nowCell || (cells.length >= 3 ? cells[2] : cells[0]);
      if(!anchor){ arrow.style.display='none'; return; }
      const wrapRect = wrap.getBoundingClientRect();
      const rect = anchor.getBoundingClientRect();
      const x = (rect.left - wrapRect.left) + (rect.width/2);
      arrow.style.left = Math.max(0, x - 8) + 'px';
      arrow.style.display = 'block';
    }catch(e){ /* no-op */ }
  }
  // Re-open/close bindings
  if (wCancel){ wCancel.setAttribute('type','button'); wCancel.addEventListener('click', (e)=>{ e.preventDefault(); closeWetModal(); }); }
  window.addEventListener('resize', placeCurrentArrow);
  // Observe #wetGrid to place arrow once items are added
  (function(){
    try{
      const grid = document.getElementById('wetGrid');
      if(!grid) return;
      const obs = new MutationObserver((mut)=>{ if(grid.children.length){ placeCurrentArrow(); obs.disconnect(); } });
      obs.observe(grid, {childList:true});
      // fallback call
      setTimeout(placeCurrentArrow, 800);
    }catch(e){}
  })();

  const SLOTS    = {{ slots|tojson|default('[]')|safe }};
  const BETS_MAP = {{ bets_map|tojson|default('{}')|safe }};
  const OBS_DATA = {{ obs_data|tojson|default('{}')|safe }};

  const parisNow = new Date(new Date().toLocaleString("en-US", {timeZone:"Europe/Paris"}));
  parisNow.setMinutes(0,0,0);
  const cutoff = new Date(parisNow.getTime() + 2*3600*1000);
  const currentIso = parisCurrentHourISO();

  const daysMap = {};
  SLOTS.forEach(s => {
    const dayKey = s.iso.slice(0, 10);
    if (!daysMap[dayKey]) daysMap[dayKey] = [];
    daysMap[dayKey].push(s);
  });

  Object.keys(daysMap).sort().forEach(dayKey => {
    const daySlots = daysMap[dayKey].sort((a,b)=>a.iso.localeCompare(b.iso));
    const card = document.createElement('div');
    card.className = 'wet-daycard';
    const [yy,mm,dd]=dayKey.split('-').map(Number);
    const d=new Date(yy,mm-1,dd);
    card.innerHTML = `<h3 class="wet-daytitle">${d.toLocaleDateString('fr-FR',{weekday:'long',day:'2-digit',month:'short'})}</h3>`;
    const inner=document.createElement('div');
    inner.className='wet-innergrid';

    for (const s of daySlots){
      const slotDate = new Date(s.iso);
      const el = document.createElement('div');
      el.className='wet-cell';
      el.dataset.iso = s.iso;

      const mine = BETS_MAP[s.iso];
      const stakedAmt    = (mine && typeof mine.amount !== 'undefined') ? mine.amount : 0;
      const stakedTarget = (mine && mine.target != null) ? Math.round(mine.target) : null;

      const hourLabel = s.iso.substring(11,13) + "h";
      const disabled  = slotDate < cutoff && !mine;
      if (disabled) el.classList.add('disabled');

      el.innerHTML = `
        <div class="wet-time">${hourLabel}</div>
        <div class="obs-rh">${s.iso === currentIso ? '⏳' : ''}</div>
        ${stakedTarget !== null ? `<div class="wet-target">${stakedTarget}%</div>` : ``}
        <div class="wet-odds">x${Number(s.odds).toFixed(1).replace('.', ',')}</div>
        ${stakedAmt > 0 ? `<div class="wet-stake">${String(stakedAmt).replace('.', ',')}</div>` : ``}
      `;

      if (!disabled){
        el.addEventListener('click', ()=>{
          wTitle.textContent = "Miser sur " + hourLabel;
          wOdds.textContent  = Number(s.odds).toFixed(1).replace('.', ',');
          wSlot.value = s.iso;
          modal.classList.add('open');
        });
      }
      inner.appendChild(el);
    }

    // Prefill with OBS_DATA
    if (OBS_DATA && typeof OBS_DATA === 'object') {
      for (const [slotIso, payload] of Object.entries(OBS_DATA)) {
        const span = inner.querySelector(`.wet-cell[data-iso="${slotIso}"] .obs-rh`);
        if (!span) continue;
        if (payload && typeof payload.humidity === 'number') {
          const newContent = Math.round(payload.humidity) + "%";
          if (span.innerHTML !== newContent) {
            span.innerHTML = newContent;
            span.classList.add('fade-in');
            setTimeout(() => span.classList.remove('fade-in'), 500);
          }
        }
      }
    }

    card.appendChild(inner);
    grid.appendChild(card);
  });

  async function backfillLastHours(n = 3) {
    if (!stationId) return;
    const cellsIso = Array.from(document.querySelectorAll('.wet-cell'))
      .map(c => c.dataset.iso).filter(Boolean).sort();
    if (cellsIso.length === 0) return;
    const idx = cellsIso.indexOf(currentIso);
    let targets = [];
    if (idx >= 0) {
      for (let k = idx; k >= 0 && targets.length < n; k--) targets.push(cellsIso[k]);
    } else {
      targets = cellsIso.slice(-n);
    }
    const params = new URLSearchParams({ station_id: stationId });
    targets.forEach(iso => params.append('slot', iso));
    try {
      const resp = await fetch(`/api/wet/observations?` + params.toString());
      if (!resp.ok) return;
      const data = await resp.json();
      console.log("[WET] API obs (backfill)", data);
      for (const [slotIso, payload] of Object.entries(data)) {
        const span = document.querySelector(`.wet-cell[data-iso="${slotIso}"] .obs-rh`);
        if (!span) continue;
        const newContent = (payload && typeof payload.humidity === 'number')
          ? payload.humidity.toFixed(0) + "%"
          : (slotIso === currentIso ? "⏳" : "");
        if (span.innerHTML !== newContent) {
          span.innerHTML = newContent;
          span.classList.add('fade-in');
          setTimeout(() => span.classList.remove('fade-in'), 500);
        }
      }
    } catch(e){ console.warn("backfillLastHours failed:", e); }
  }

  async function refreshHumidityResults() {
    if (!stationId) return;
    const span = document.querySelector(`.wet-cell[data-iso="${currentIso}"] .obs-rh`);
    if (!span) return;
    const params = new URLSearchParams({ station_id: stationId });
    params.append('slot', currentIso);
    try {
      const resp = await fetch(`/api/wet/observations?` + params.toString());
      if (!resp.ok) return;
      const data = await resp.json();
      console.log("[WET] API obs (refresh)", data);
      const payload = data[currentIso] ?? null;
      const newContent = (payload && typeof payload.humidity === 'number')
        ? payload.humidity.toFixed(0) + "%"
        : "⏳";
      if (span.innerHTML !== newContent) {
        span.innerHTML = newContent;
        span.classList.add('fade-in');
        setTimeout(() => span.classList.remove('fade-in'), 500);
      }
    } catch(e){ console.warn("refreshHumidityResults failed:", e); }
  }

  document.addEventListener("DOMContentLoaded", async () => {
    await backfillLastHours(3);
    await refreshHumidityResults();
  });
  setInterval(refreshHumidityResults, 60000);

})();

// ---------- Menu utilisateur (topbar) ----------
(function(){
  const btn = document.getElementById('userMenuBtn');
  const dd  = document.getElementById('userDropdown');
  if (!btn || !dd) return;
  function closeMenu(){ dd.classList.remove('open'); btn.setAttribute('aria-expanded','false'); }
  function toggleMenu(){
    const isOpen = dd.classList.toggle('open');
    btn.setAttribute('aria-expanded', isOpen ? 'true' : 'false');
  }
  btn.addEventListener('click', (e) => { e.stopPropagation(); toggleMenu(); });
  document.addEventListener('click', () => closeMenu());
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeMenu(); });
})();
</script>

</body></html>
"""

YOUBET_HTML = """
<!doctype html><html lang="fr"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>You Bet</title>
{{ css|safe }}
<style>
  :root {
    --fog1: rgba(255,255,255,.06);
    --fog2: rgba(255,255,255,.10);
  }
  html, body { height:100%; margin:0; background:#0b1020; }
  .intro-wrap{
    position:relative; height:100%;
    display:flex; align-items:center; justify-content:center;
    overflow:hidden;
  }
  .fog{
    position:absolute; inset:0;
    background:
      radial-gradient(60% 40% at 20% 30%, var(--fog2), transparent 60%),
      radial-gradient(50% 35% at 80% 70%, var(--fog1), transparent 60%),
      radial-gradient(70% 45% at 50% 50%, var(--fog1), transparent 70%);
    filter: blur(20px);
    animation: drift 16s linear infinite alternate;
  }
  @keyframes drift {
    from { transform: translate3d(-2%, -2%, 0) scale(1.04); }
    to   { transform: translate3d( 2%,  2%, 0) scale(1.06); }
  }

  /* Bigger logo */
  .logo{
    position:relative; z-index:1;
    width:min(520px, 85vw);     /* bigger than before */
    user-select:none; -webkit-user-drag:none;
  }

  /* Bigger, higher button */
  .fallback{
    position:absolute; bottom:160px;    /* higher than before */
    left:0; right:0; text-align:center;
    display:none;
    z-index:2;
  }
  .fallback button{
    background:#64b5f6;
    color:#0b1020;
    border:0;
    border-radius:18px;
    padding:18px 32px;
    font-size:20px;
    font-weight:800;
    cursor:pointer;
    box-shadow:0 10px 34px rgba(187,134,252,.45);
    transition:transform .12s ease, filter .12s ease;
  }
  .fallback button:hover{
    transform:scale(1.05);
    filter: brightness(1.05);
  }

  .hint{
    position:absolute; bottom:22px; left:0; right:0; text-align:center;
    font-size:12px; color:#9fb3c8; opacity:.8; z-index:1;
  }
  .backlink{
    position:absolute; bottom:84px; left:0; right:0; text-align:center;
    z-index:2;
  }
  .backlink a{
    color:#9fb3c8; text-decoration:none; font-weight:700;
  }
  .backlink a:hover{ color:#cfe7ff; }  
</style>
</head><body>
<div class="intro-wrap">
  <div class="fog"></div>
  <img class="logo" src="{{ url_for('static', filename='img/you_bet.png') }}" alt="You Bet">
  <div class="fallback"><button id="playBtn">Yes, I'm God</button></div>
  <div class="backlink"><a id="backLink" href="#">Retour</a></div>
  <div class="hint">Un instant…</div>
</div>

<script>
(function(){
  // ----- Config -----
  const MIN_SHOW_MS = 2000;   // minimum time to show this page
  const MAX_WAIT_MS = 5000;   // hard timeout: go back even if no sound
  const AUDIO_SRC  = "{{ url_for('static', filename='audio/yoga.wav') }}";

  // read ?back=/ppp or ?next=/ppp (accept both)
  const sp = new URLSearchParams(location.search);
  const backUrl = sp.get('back') || sp.get('next') || '/ppp';
  const backLink = document.getElementById('backLink');
  if (backLink) backLink.href = backUrl;

  const startTs = performance.now();
  let finished = false;

  function goBack() {
    if (finished) return;
    finished = true;
    // ensure min duration is respected
    const elapsed = performance.now() - startTs;
    const left = Math.max(0, MIN_SHOW_MS - elapsed);
    setTimeout(() => { window.location.href = backUrl; }, left);
  }

  // Create audio element (tag form tends to be more consistent than new Audio in Safari)
  const audio = document.createElement('audio');
  audio.src = AUDIO_SRC;
  audio.preload = 'auto';
  audio.playsInline = true;       // iOS-friendly
  audio.controls = false;         // hidden
  audio.style.display = 'none';
  document.body.appendChild(audio);

  // If autoplay is blocked, show the manual button
  const fallback = document.querySelector('.fallback');
  const playBtn  = document.getElementById('playBtn');

  function tryPlay() {
    // Always reset to start to avoid partial leftovers
    try { audio.pause(); audio.currentTime = 0; } catch(e) {}
    return audio.play();
  }

  // When it ends: go back (respecting MIN_SHOW_MS)
  audio.addEventListener('ended', goBack, { once:true });

  // Hard timeout: go back even if no audio fired
  setTimeout(goBack, MAX_WAIT_MS);

  // Attempt autoplay shortly after load (let the page render a bit)
  setTimeout(() => {
    tryPlay().then(() => {
      // Autoplay worked: ensure fallback stays hidden
      if (fallback) fallback.style.display = 'none';
    }).catch(() => {
      // Autoplay blocked → show the button
      if (fallback) fallback.style.display = 'block';
    });
  }, 150);

  // Manual play button
  if (playBtn) {
    playBtn.addEventListener('click', () => {
      tryPlay().then(()=>{
        // hide button once playing
        if (fallback) fallback.style.display = 'none';
      }).catch(()=>{ /* still blocked; keep button visible */ });
    });
  }
})();
</script>
</body></html>
"""

ADMIN_HTML = """
<!doctype html><html lang='fr'><head>
<meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'>
{{ css|safe }}<title>Admin</title>
</head><body>
<div class="stars"></div>
<nav>
  <div class="container topbar">
    <!-- Left: main links -->
    <div class="nav-left">
      <a class="brand" href="/">Humeur</a>
      <a href="/meteo" style="color:#ffd95e;">Météo</a>
      {% if current_user.is_authenticated %}
        <a href="/allocate">Attribuer (initial)</a>
        <a href="/stake">Remiser</a>
        {% if current_user.is_admin %}<a href="/admin">Admin</a>{% endif %}
      {% endif %}
    </div>

    <!-- Center: Solde -->
    <div class="nav-center">
      {% if current_user.is_authenticated and solde_str %}
        <div class="solde-box" title="Points restants Humeur + Météo">
          <span class="solde-label">Solde&nbsp;:</span>
          <span class="solde-value">{{ solde_str }}</span>
        </div>
      {% endif %}
    </div>

    <div class="nav-right">
      <a class="btn ppp-btn" href="/ppp" title="Zeus">Pluie Pas Pluie</a>
      {% if current_user.is_authenticated %}
        <span><strong>{{ current_user.username }}</strong></span>
        <a href="/logout">Se déconnecter</a>
      {% else %}
        <a href="/register">Créer un compte</a>
        <a href="/login">Se connecter</a>
      {% endif %}
    </div>

    <!-- Right: auth -->
    <div class="nav-right">
      {% if current_user.is_authenticated %}
        <span><strong>{{ current_user.username }}</strong></span>
        <a href="/logout">Se déconnecter</a>
      {% else %}
        <a href="/register">Créer un compte</a>
        <a href="/login">Se connecter</a>
      {% endif %}
    </div>
  </div>
</nav>

<div class='container grid' style='margin-top:16px;'>
  <div class='card'>
    <h3>Planifier des valeurs</h3>
    <form method='post'>
      <div class='grid cols-2'>
        <div><label>Date</label><input type='date' name='the_date' required></div>
        <div><label>Pierre</label><input type='number' step='0.01' min='0' name='pierre_value' required></div>
        <div><label>Marie</label><input type='number' step='0.01' min='0' name='marie_value' required></div>
      </div>
      <div style='margin-top:12px;'><button class='btn primary' type='submit'>Enregistrer / Remplacer</button></div>
    </form>
  </div>

  <div class='card'>
    <h3>Historique publié</h3>
    <table class='table'>
      <tr><th>Date</th><th>Pierre</th><th>Marie</th><th>Publié à</th></tr>
      {% for d in published %}
        <tr>
          <td>{{ d.the_date }}</td>
          <td>{{ d.pierre_value }}</td>
          <td>{{ d.marie_value }}</td>
          <td>{{ d.published_at }}</td>
        </tr>
      {% else %}
        <tr><td colspan='4'><em>Rien.</em></td></tr>
      {% endfor %}
    </table>
  </div>
</div>
</body></html>
"""

METEO_HTML = """
<!doctype html><html lang='fr'><head>
<meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'>
<title>Météo</title>
{{ css|safe }}
<style>
.meteo-title{ color:#ffd95e; text-shadow:0 0 18px rgba(255,217,94,.25); }
.badge{display:inline-block;padding:4px 8px;border-radius:999px;border:1px solid rgba(255,255,255,.14);font-size:12px;color:var(--muted)}
.forecast-grid{display:grid;grid-template-columns:repeat(5,1fr);gap:10px}
@media(max-width:900px){ .forecast-grid{grid-template-columns:repeat(2,1fr)} }
.tile{background:var(--card-bg);border:1px solid var(--card-border);border-radius:12px;padding:10px}
</style>
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
</head><body>
<div class="stars"></div>
<nav>
  <div class="container topbar">
    <!-- Left: main links -->
    <div class="nav-left">
      <a class="brand" href="/">Humeur</a>
      <a href="/meteo" style="color:#ffd95e;">Météo</a>
      {% if current_user.is_authenticated %}
        <a href="/allocate">Attribuer (initial)</a>
        <a href="/stake">Remiser</a>
        {% if current_user.is_admin %}<a href="/admin">Admin</a>{% endif %}
      {% endif %}
    </div>

    <!-- Center: Solde -->
    <div class="nav-center">
      {% if current_user.is_authenticated and solde_str %}
        <div class="solde-box" title="Points restants Humeur + Météo">
          <span class="solde-label">Solde&nbsp;:</span>
          <span class="solde-value">{{ solde_str }}</span>
        </div>
      {% endif %}
    </div>

    <div class="nav-right">
      <a class="btn ppp-btn" href="/ppp" title="Zeus">Pluie Pas Pluie</a>
      {% if current_user.is_authenticated %}
        <span><strong>{{ current_user.username }}</strong></span>
        <a href="/logout">Se déconnecter</a>
      {% else %}
        <a href="/register">Créer un compte</a>
        <a href="/login">Se connecter</a>
      {% endif %}
    </div>    

    <!-- Right: auth -->
    <div class="nav-right">
      {% if current_user.is_authenticated %}
        <span><strong>{{ current_user.username }}</strong></span>
        <a href="/logout">Se déconnecter</a>
      {% else %}
        <a href="/register">Créer un compte</a>
        <a href="/login">Se connecter</a>
      {% endif %}
      <!-- Logo à droite -->
      <img src="{{ url_for('static', filename='img/weather_bets_S.png') }}" 
        alt="Meteo God" 
        class="topbar-logo">
    </div>         
  </div>
</nav>

<div class='container' style='margin-top:16px;'>
  <div class='card'>
    <h2 class="meteo-title">Météo — heures cumulées (3 derniers jours)</h2>
    <form id="cityForm" class="grid" style="grid-template-columns:1fr auto;gap:12px;margin-bottom:10px">
      <input type="text" id="cityInput" placeholder="Paris, France" value="{{ default_city }}">
      <button class="btn" type="submit">Afficher</button>
    </form>
    <div class="badge" id="cityBadge"></div>
    <div class='grid' style='margin-top:12px;grid-template-columns:1fr 1fr;gap:12px'>
      <div class='tile'>
        <h3 style='margin:0 0 6px'>Derniers 3 jours</h3>
        <div id="last3"></div>
      </div>
      <div class='tile'>
        <h3 style='margin:0 0 6px'>Prévision 5 jours</h3>
        <div id="forecast"></div>
      </div>
    </div>
    <div class='tile' style='margin-top:12px'>
      <h3 style='margin:0 0 6px'>Graphique (heures cumulées)</h3>
      <div id="chartWrap" style="height:260px"><canvas id="meteoChart"></canvas></div>
    </div>
  </div>
</div>

<script>
let _chart;
function renderChart(series){
  const ctx = document.getElementById('meteoChart').getContext('2d');
  if(_chart) _chart.destroy();
  _chart = new Chart(ctx, {
    type:'line',
    data:{
      labels: series.labels,
      datasets:[
        {label:'Soleil (h)', data: series.sun, tension:0.25, pointRadius:2},
        {label:'Pluie (h)',  data: series.rain, tension:0.25, pointRadius:2}
      ]
    },
    options:{
      responsive:true, maintainAspectRatio:false, animation:false,
      plugins:{ legend:{labels:{color:'#e8ecf2'}}, tooltip:{titleColor:'#e8ecf2',bodyColor:'#e8ecf2',backgroundColor:'rgba(17,22,36,.9)'} },
      scales:{
        x:{ ticks:{ color:'#a8b0c2' }, grid:{ color:'rgba(255,255,255,.06)'} },
        y:{ ticks:{ color:'#a8b0c2' }, grid:{ color:'rgba(255,255,255,.06)'} }
      }
    }
  });
}

async function loadCity(city){
  const badge = document.getElementById('cityBadge');
  badge.textContent = 'Chargement…';
  const t = await fetch(`/api/meteo/today?city=${encodeURIComponent(city)}`).then(r=>r.json());
  if(t.error){ badge.textContent = 'Ville introuvable'; return; }
  badge.textContent = `${t.city} — ${t.date}`;

  const f = await fetch(`/api/meteo/forecast5?city=${encodeURIComponent(city)}`).then(r=>r.json());
  const fc = f.forecast5 || [];

  // Fill last3
  document.getElementById('last3').innerHTML =
    `<table class="table">
       <tr><th>Soleil (3j)</th><td><strong>${t.sun_hours_3d}</strong> h</td></tr>
       <tr><th>Pluie (3j)</th><td><strong>${t.rain_hours_3d}</strong> h</td></tr>
     </table>`;

  // Fill forecast list (5 days)
  document.getElementById('forecast').innerHTML =
    '<table class="table"><tr><th>Jour</th><th>Soleil (h)</th><th>Pluie (h)</th><th>Min/Max (°C)</th></tr>' +
    fc.map(d => `<tr><td>${d.date}</td><td>${d.sun_hours}</td><td>${d.rain_hours}</td><td>${d.t_min} / ${d.t_max}</td></tr>`).join('') +
    '</table>';

  // Build chart series: last3 “Aujourd’hui” then next 5 days predicted
  const labels = ['Aujourd’hui (3j)'].concat(fc.map(d=>d.date));
  const sun = [t.sun_hours_3d].concat(fc.map(d=>d.sun_hours));
  const rain= [t.rain_hours_3d].concat(fc.map(d=>d.rain_hours));
  renderChart({labels, sun, rain});
}

document.getElementById('cityForm').addEventListener('submit', (e)=>{
  e.preventDefault();
  const city = document.getElementById('cityInput').value.trim();
  if(city) loadCity(city);
});

window.addEventListener('DOMContentLoaded', ()=>{
  const def = document.getElementById('cityInput').value.trim() || 'Paris, France';
  loadCity(def);
});
</script>
</body></html>
"""

CARTE_HTML = """
<!doctype html><html lang='fr'><head>
<meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'>
<title>Carte</title>
{{ css|safe }}

<!-- Leaflet -->
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>

<style>
  /* Lien "Carte" vert dans la topbar */
  .brand-map { color:#30d158; font-weight:800; text-decoration:none; }
  .brand-map:hover { color:#7ef5a5; }
  .brand-map.active { text-shadow:0 0 12px rgba(48,209,88,.35); }

  /* Layout carte + panneau */
  .layout { display:grid; grid-template-columns: 1fr 360px; gap:16px; margin-top:16px; }
  #worldMap {
    height: calc(100vh - 120px);
    min-height: 520px;
    border-radius: 16px;
    overflow: hidden;
  }

  /* Halo futuriste dans un pane Leaflet dédié (.fx-pane) */
  #worldMap .leaflet-pane.fx-pane .fx-halo{
    position:absolute; inset:0; pointer-events:none;
    background:
      radial-gradient(80% 60% at 50% 10%, rgba(33,96,243,.22), transparent 60%),
      radial-gradient(70% 50% at 50% 100%, rgba(0,255,195,.16), transparent 55%),
      repeating-linear-gradient(90deg, rgba(120,180,255,.06) 0 1px, transparent 1px 40px);
    mix-blend-mode: screen; /* se mélange aux tuiles sombres */
  }

  /* Fond + lisibilité */
  .leaflet-container { background:#0e1627; filter: brightness(1.18) saturate(1.05) contrast(1.03); }

  /* Fuseaux horaires */
  .tz-line { opacity:.22; }
  .tz-line.thick { opacity:.34; }

  /* Panneau latéral (sélection stations) */
  .panel {
    background: rgba(18,26,44,.75);
    border:1px solid rgba(120,180,255,.15);
    border-radius:16px; padding:14px; backdrop-filter: blur(6px);
  }
  .panel h3 { margin: 6px 0 10px; color:#cfe6ff; }
  .panel label { font-size:14px; color:#9fb5d1; display:block; margin-bottom:6px; }
  .panel input {
    width:100%; padding:10px; background:#0e1627;
    border:1px solid rgba(120,180,255,.25); border-radius:10px; color:#eaf3ff;
  }
  .panel .muted { color:#9fb5d1; font-size:12px; margin-top:6px; }

  .list { margin-top:10px; max-height: 50vh; overflow:auto; }
  .item {
    display:flex; align-items:center; justify-content:space-between;
    background:#0e1627; border:1px solid rgba(120,180,255,.15);
    border-radius:10px; padding:10px; margin-bottom:8px;
  }
  .item .lbl { color:#eaf3ff; font-weight:600; margin-right:10px; }
  .item button {
    background:#306bd1; color:#041021; border:none; border-radius:10px;
    padding:8px 12px; font-weight:700; cursor:pointer;
  }
  .item button:hover{ filter:brightness(1.05); }

    /* Bouton Partir (plus discret) */
  .btn-partir {
    background: #888;      /* gris neutre */
    color: #fff;
    border: none;
    border-radius: 12px;
    padding: 4px 10px;
    font-size: 0.85rem;
    font-weight: 600;
    cursor: pointer;
    opacity: 0.50; /* 👈 rend le bouton plus transparent */
    transition: background 0.2s ease, opacity 0.2s ease;
  }
  .btn-partir:hover {
    background: #666;      /* un peu plus foncé au survol */
    opacity: 1; /* 👈 survol = bouton pleinement visible */
  }

    /* Forcer les boutons "Partir" en gris, quoi qu'il arrive */
  .list .btn-partir,
  button.btn-partir {
    background: #888 !important;
    color: #041021 !important;
    border: none !important;
    box-shadow: none !important;
    border-radius: 12px;
    padding: 4px 10px;
    font-size: 0.85rem;
    font-weight: 600;
    cursor: pointer;
    transition: background .2s ease, opacity .2s ease;
  }
  .list .btn-partir:hover,
  button.btn-partir:hover {
    background: #666 !important;
  }

  /* Drapeau station */
  .flag { font-size:20px; line-height:1; }

  /* Curseur "main" et survol avec le même effet que les mises */
  #myStationsList .item .lbl {
    cursor: pointer;
    transition: color 0.2s ease, text-shadow 0.2s ease;
  }
  #myStationsList .item .lbl:hover {
    color: #30d158; /* même vert que les mises */
    text-shadow: 0 0 6px rgba(48, 209, 88, 0.7), 0 0 12px rgba(48, 209, 88, 0.4);
  }

  /* Aide visuelle sur résultat vide */
  .empty { color:#9fb5d1; font-size:13px; padding:8px; text-align:center; }
  .wx{
  font-size: 22px;
  filter: drop-shadow(0 0 6px rgba(120,180,255,.35));
  user-select: none;
  pointer-events: none; /* le clic reste sur le marqueur */
  }
  .user-menu { position: relative; display: inline-block; }
  .user-trigger{
    background: transparent; border: 0; color: #fff; font-weight: 800;
    cursor: pointer; display: inline-flex; align-items: center; gap: 6px;
  }
  .user-trigger .caret{ opacity: .8; font-size: 12px; }
  .user-dropdown{
    position: absolute; right: 0; top: 120%;
    background: rgba(13,20,40,.98);
    border: 1px solid rgba(255,255,255,.08);
    border-radius: 12px;
    box-shadow: 0 10px 28px rgba(0,0,0,.35);
    min-width: 180px; padding: 6px; display: none; z-index: 1000;
    backdrop-filter: blur(6px);
  }
  .user-dropdown.open{ display: block; }
  .user-dropdown .item{
    display: block; width: 100%; text-align: left;
    padding: 10px 12px; border-radius: 10px;
    background: transparent; color: #cfe3ff; text-decoration: none;
    border: 0; cursor: pointer; font-weight: 700;
  }
  .user-dropdown .item:hover{ background: rgba(120,180,255,.12); color: #79e7ff; }
  .user-dropdown .item.disabled{
    opacity: .5; cursor: default; pointer-events: none;
  }
</style>
</head><body>
<div class="stars"></div>

<nav>
  <div class="container topbar">
    <div class="nav-left">
      <a href="/ppp" class="topbar-logo-link" aria-label="Rafraîchir la page PPP">
        <img src="{{ url_for('static', filename='img/weather_bets_S.png') }}" alt="Meteo God" class="topbar-logo">
      </a>
    </div>
    <div class="nav-center">
      {% if current_user.is_authenticated and solde_str %}
        <div class="solde-box">
          <span class="solde-label">Solde&nbsp;:</span>
          <span class="solde-value">{{ solde_str }}</span>
        </div>
      {% endif %}
    </div>
    <div class="nav-right">
      {% if current_user.is_authenticated %}
        <div class="user-menu">
          <button class="user-trigger" id="userMenuBtn" aria-haspopup="true" aria-expanded="false">
            <strong>{{ current_user.username }}</strong>
            <span class="caret">▾</span>
          </button>
          <div class="user-dropdown" id="userDropdown" role="menu">
            <a class="item" href="{{ url_for('trade_page') }}">Échanges 🤝</a>
            <a class="item" href="/static/dessin/dessin.html">Offrandes 🎨</a>
            <a class="item" href="{{ url_for('cabine_page') }}">Profil 👔</a>            
            <a class="item" href="/carte">Carte 🗺️</a>
            <a class="item" href="{{ url_for('wet') }}">Humidité 💧</a>
            <a class="item" href="/logout">Se déconnecter</a>
          </div>
        </div>
      {% else %}
        <a href="/register">Créer un compte</a>
        <a href="/login">Se connecter</a>
      {% endif %}      
      <a id="trade-unread"
         class="badge-unread"
         href="{{ url_for('trade_page') }}"
         aria-label="Aller au marché (Trade)"
         style="display:none; margin-left:.5rem;">
        NOUVEAU MESSAGE
      </a>
      </span>
    </div>
  </div>
</nav>

<div class="container layout">
  <div id="worldMap"></div>

  <aside class="panel">
    <h3>Ajouter une station météo</h3>
    <label for="q">Ville (France) ou n° de département</label>
    <input id="q" type="text" placeholder="Ex: Annecy ou 74" autocomplete="off">
    <div class="muted">Saisissez au moins 2 caractères pour lancer la recherche.</div>

    <!-- Résultats de recherche -->
    <div class="list" id="stationList">
      <div class="empty">Aucun résultat pour l’instant…</div>
    </div>

    <!-- Vos villes sélectionnées -->
    <div class="selbox" style="margin-top:16px;">
      <div class="selbox-title">Vos villes</div>
      <div id="myStationsList" class="list">
        <div class="empty">Aucune ville pour l’instant.</div>
      </div>
    </div>
  </aside>
</div>

<script>
(function(){
  // ----------------- MAP + HALO + TUILES (inchangé) -----------------
  const map = L.map('worldMap', {
    worldCopyJump: true, zoomControl: true, scrollWheelZoom: true, attributionControl: true
  }).setView([46.5, 2.5], 5);

  L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_nolabels/{z}/{x}/{y}{r}.png',{
    subdomains:'abcd', maxZoom:19, opacity:0.95,
    attribution:'&copy; <a href="https://www.openstreetmap.org/">OSM</a> &copy; <a href="https://carto.com/">CARTO</a>'
  }).addTo(map);

  const fxPane = map.createPane('fx'); fxPane.style.zIndex = 350; fxPane.style.pointerEvents='none';
  L.DomUtil.create('div','fx-halo',fxPane);
  L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_only_labels/{z}/{x}/{y}{r}.png',
    {subdomains:'abcd', maxZoom:19, opacity:.96, pane:'overlayPane'}).addTo(map);

  // (fuseaux etc… conservés si tu les avais)

  // --- ÉTAT global sûr (évite ReferenceError si déjà défini ailleurs) ---
  window._markers    = window._markers    || new Map();  // id -> Leaflet marker
  window._myStations = window._myStations || new Map();  // id -> {id,label,lat,lon}
  const markers    = window._markers;
  const myStations = window._myStations;

  // --- helpers ---
  function escapeHtml(str){
    return String(str).replace(/[&<>"']/g, m => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));
  }
  function normalizeStation(s){
    // id tolère id/icao/code/station_id
    const id   = s.id || s.icao || s.code || s.station_id;

    // Essaie d’extraire la ville proprement
    let city = s.city || s.ville || null;
    // Si pas de champ city, tente de la déduire du label "Nom — Ville (dept)"
    if (!city && typeof s.label === 'string') {
      const parts = s.label.split('—');
      if (parts.length > 1) {
        city = parts[1].trim().replace(/\\(.*\\)$/, '').trim() || null;
      }
    }

    // Label lisible
    const name  = s.name || '';
    const label = s.label || (name && city ? `${name} — ${city}` : (city || name || id));

    // Coordonnées
    const lat = (typeof s.lat === 'number') ? s.lat : (s.latitude ?? null);
    const lon = (typeof s.lon === 'number') ? s.lon : (s.longitude ?? null);

    return id ? { id, city, label, lat, lon } : null;
  }

  async function emojiForCity(city){
    if (!city) return '❓';
    try{
      const r = await fetch('/api/meteo/today?city=' + encodeURIComponent(city + ', France'));
      if (!r.ok) return '❓';
      const data = await r.json();
      let isRain = false;
      if (data.pop != null){
        let pop = +data.pop; if (pop > 1) pop /= 100;
        isRain = pop >= 0.45;
      } else {
        isRain = (+data.rain_hours >= 4) || (+data.code >= 60);
      }
      return isRain ? '💧' : '☀️';
    } catch(e){
      console.warn('emojiForCity error', e);
      return '❓';
    }
  }  

  // --- marqueurs carte ---
  function addWeatherMarker(s){
    if (!s || !s.lat || !s.lon) return;
    if (markers.has(s.id)) return;

    // Icône provisoire en attendant la réponse API
    const icon = L.divIcon({
      html: '<span class="wx">…</span>',
      className: '',
      iconSize: [24, 24],
      iconAnchor: [12, 16]
    });

    const m = L.marker([s.lat, s.lon], { icon, title: s.label || s.id }).addTo(map);
    m.on('click', () => window.location.href = '/ppp/' + encodeURIComponent(s.id));
    markers.set(s.id, m);

    // Met à jour l’emoji depuis l’API
    updateWeatherIcon(s.id, s.city);
  }

  async function updateWeatherIcon(id, city){
    const m = markers.get(id);
    if (!m) return;
    const emoji = await emojiForCity(city);
    const el = document.createElement('div');
    el.innerHTML = `<span class="wx">${emoji}</span>`;
    const newIcon = L.divIcon({ html: el.innerHTML, className: '', iconSize:[24,24], iconAnchor:[12,16] });
    m.setIcon(newIcon);
  }
  function removeFlagMarker(id){
    const m = markers.get(id);
    if (m){ map.removeLayer(m); markers.delete(id); }
  }

  // ----------------- Rendu de la liste de raccourcis -----------------
  const myList = document.getElementById('myStationsList');

  function renderSelectedList(){
    if (!myList) return;
    const arr = Array.from(myStations.values()).sort((a,b)=> (a.label||'').localeCompare(b.label||''));
    if (!arr.length){
      myList.innerHTML = '<div class="empty">Aucune ville pour l’instant.</div>';
      return;
    }
    myList.innerHTML = '';
    for (const s of arr){
      const row = document.createElement('div');
      row.className = 'item';
      row.innerHTML = `
        <div class="lbl" title="Ouvrir le calendrier">${escapeHtml(s.label || s.id)}</div>
        <button class="btn-partir" type="button" title="Retirer" data-id="${s.id}">Partir</button>
      `;
      row.querySelector('.lbl').addEventListener('click', () => {
        window.location.href = '/ppp/' + encodeURIComponent(s.id);
      });
      row.querySelector('.btn-partir').addEventListener('click', async () => {
        try{
          const r = await fetch('/api/my_stations/' + encodeURIComponent(s.id), { method:'DELETE' });
          if (!r.ok) console.error('DELETE /api/my_stations failed', r.status);
          myStations.delete(s.id);
          removeFlagMarker(s.id);
          renderSelectedList();
        }catch(e){ console.error(e); }
      });
      myList.appendChild(row);
    }
  }

  // ----------------- Charger mes stations persistées -----------------
  fetch('/api/my_stations')
    .then(r => r.ok ? r.json() : {stations:[]})
    .then(json => {
      const arr = Array.isArray(json.stations) ? json.stations : [];
      arr.forEach(raw => {
        const s = normalizeStation(raw);
        if (!s){ console.warn('station invalide côté /api/my_stations:', raw); return; }
        myStations.set(s.id, s);
        addWeatherMarker(s);
      });
      renderSelectedList();
    })
    .catch(e => console.error('/api/my_stations error', e));

  // ----------------- Recherche stations -----------------
  const q    = document.getElementById('q');
  const list = document.getElementById('stationList');
  let debounce;

  q.addEventListener('input', () => {
    clearTimeout(debounce);
    const val = (q.value || '').trim();
    if (val.length < 2){
      list.innerHTML = '<div class="empty">Saisissez au moins 2 caractères…</div>';
      return;
    }
    debounce = setTimeout(() => searchStations(val), 220);
  });

  async function searchStations(term){
    try{
      const r = await fetch('/api/stations?q=' + encodeURIComponent(term));
      const json = await r.json();
      const items = Array.isArray(json.stations) ? json.stations : (Array.isArray(json.items) ? json.items : []);
      renderResults(items);
    }catch(e){
      console.error('stations search error', e);
      list.innerHTML = '<div class="empty">Erreur de recherche.</div>';
    }
  }

  function renderResults(items){
    list.innerHTML = '';
    if (!items.length){
      list.innerHTML = '<div class="empty">Aucun résultat.</div>';
      return;
    }
    items.forEach(raw => {
      const s = normalizeStation(raw);
      if (!s){ console.warn('station invalide côté /api/stations:', raw); return; }
      const added = myStations.has(s.id);

      const el = document.createElement('div');
      el.className = 'item';
      el.innerHTML = `
        <div class="lbl">${escapeHtml(s.label)}</div>
        ${added
          ? `<button class="btn-partir" type="button" data-id="${s.id}">Partir</button>`
          : `<button class="btn" type="button" data-id="${s.id}">Gérer</button>`}
      `;
      const btn = el.querySelector('button');

      // ouvrir le calendrier au clic sur le label
      el.querySelector('.lbl').addEventListener('click', () => {
        window.location.href = '/ppp/' + encodeURIComponent(s.id);
      });

      if (!added){
        // --- Gérer -> ajoute la station ---
        btn.addEventListener('click', async () => {
          try{
            const r = await fetch('/api/my_stations', {
              method:'POST',
              headers:{'Content-Type':'application/json'},
              body: JSON.stringify({ id:s.id, label:s.label, lat:s.lat, lon:s.lon })
            });
            if (!r.ok) { console.error('POST /api/my_stations failed', r.status); return; }
            myStations.set(s.id, s);
            addWeatherMarker(s);
            // bascule visuelle immédiate
            btn.textContent = 'Partir';
            btn.className = 'btn-partir';
            renderSelectedList();
            if (s.lat && s.lon) map.flyTo([s.lat, s.lon], 9, {duration:0.6});
          }catch(e){ console.error(e); }
        });
      } else {
        // --- Partir -> retire la station ---
        btn.addEventListener('click', async () => {
          try{
            const r = await fetch('/api/my_stations/' + encodeURIComponent(s.id), { method:'DELETE' });
            if (!r.ok) console.error('DELETE /api/my_stations failed', r.status);
          }catch(e){ console.error(e); }
          myStations.delete(s.id);
          removeFlagMarker(s.id);
          btn.textContent = 'Gérer';
          btn.className = 'btn';
          renderSelectedList();
        });
      }

      list.appendChild(el);
    });
  }
})();
// ---------- Menu utilisateur (topbar) ----------
(function(){
  const btn = document.getElementById('userMenuBtn');
  const dd  = document.getElementById('userDropdown');
  if (!btn || !dd) return;

  function closeMenu(){
    dd.classList.remove('open');
    btn.setAttribute('aria-expanded','false');
  }
  function toggleMenu(){
    const isOpen = dd.classList.toggle('open');
    btn.setAttribute('aria-expanded', isOpen ? 'true' : 'false');
  }

  btn.addEventListener('click', (e) => {
    e.stopPropagation();
    toggleMenu();
  });
  document.addEventListener('click', () => closeMenu());
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') closeMenu();
  });
})();
</script>
</body></html>
"""

# -----------------------------------------------------------------------------
# Routes publiques API/UI
# -----------------------------------------------------------------------------
@app.route('/')
def index():
    return redirect(url_for('intro'))

@app.route('/api/moods')
def api_moods():
    rows = DailyMood.query.order_by(DailyMood.the_date.asc()).all()
    return jsonify([
        { 'date': r.the_date.isoformat(), 'pierre': r.pierre_value, 'marie': r.marie_value }
        for r in rows
    ])

@app.route('/api/today')
def api_today():
    d = today_paris()
    r = last_published_on_or_before(d)
    if not r:
        return jsonify({})
    return jsonify({
        'date': r.the_date.isoformat(),
        'pierre': r.pierre_value,
        'marie': r.marie_value,
        'published_at': r.published_at.astimezone(APP_TZ).strftime('%Y-%m-%d %H:%M'),
        'note': "Valeur du jour indisponible — utilisation de la dernière valeur publiée"
                if r.the_date != d else "Valeur publiée aujourd'hui"
    })

@app.route('/api/me')
@login_required
def api_me():
    positions = Position.query.filter_by(user_id=current_user.id, status='ACTIVE').all()
    return jsonify({
        'bal_pierre': float(current_user.bal_pierre or 0.0),
        'bal_marie': float(current_user.bal_marie or 0.0),
        'positions': [
            {
                'id': p.id, 'asset': p.asset, 'principal_points': p.principal_points,
                'start_value': p.start_value, 'start_date': p.start_date.isoformat(),
                'maturity_date': p.maturity_date.isoformat(), 'status': p.status,
            } for p in positions
        ]
    })

@app.route('/api/meteo/today')
def api_meteo_today():
    city = (request.args.get("city") or "").strip()
    refresh = request.args.get("refresh") == "1"
    if not city:
        return jsonify({"error":"city required"}), 400
    d = today_paris()
    snap = get_city_snapshot(city, d, force_refresh=refresh)  # <-- add param
    if not snap:
        return jsonify({"error":"city not found"}), 404
    return jsonify({
        "city": city,
        "date": d.isoformat(),
        "sun_hours_3d": snap.sun_hours_3d,
        "rain_hours_3d": snap.rain_hours_3d,
        "lat": snap.lat, "lon": snap.lon
    })

@app.route('/api/meteo/forecast5')
def api_meteo_forecast5():
    city = (request.args.get("city") or "").strip()
    refresh = request.args.get("refresh") == "1"
    if not city:
        return jsonify({"error": "city required"}), 400

    d = today_paris()
    snap = get_city_snapshot(city, d, force_refresh=refresh)
    if not snap:
        return jsonify({"error": "city not found"}), 404

    j = json.loads(snap.forecast_json)

    #  guard against list vs dict
    if isinstance(j, list):
        j = {"forecast5": j}

    return jsonify({"city": city, **j})

@app.route('/meteo')
def meteo():
    flash("La section Météo a été retirée. Tout se passe désormais dans « Pluie Pas Pluie ».")
    return redirect(url_for('ppp'))

@app.route('/intro')
def intro():
    return render_template_string(INTRO_HTML, css=BASE_CSS)

@app.route('/initiale')
def initiale_page():
    return render_template('initiale.html')

@app.route('/youbet')
def you_bet():
    back = request.args.get('back') or url_for('ppp')
    return render_template_string(YOUBET_HTML, css=BASE_CSS, back=back)

# -----------------------------------------------------------------------------
# Auth minimal
# -----------------------------------------------------------------------------
@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'GET':
        body = """
        <form method=post>
          <label>Pseudo</label>
          <input name="username" maxlength="40" required>
          <label>Email</label><input type="email" name="email" required>
          <label>Mot de passe</label><input type="password" name="password" required>
          <div style='margin-top:12px;'><button class='btn primary'>Créer le compte</button></div>
        </form>"""
        return render(AUTH_HTML, css=BASE_CSS, title='Créer un compte', body=body)

    from sqlalchemy import or_, func

    # --- Entrées ---
    username = (request.form.get('username') or '').strip()
    email_raw = (request.form.get('email') or '').strip()
    email = email_raw.lower()
    password = (request.form.get('password') or '').strip()

    # --- Normalisation du pseudo : 1re lettre -> majuscule si c'est une lettre ---
    if username:
        first = username[0]
        if first.isalpha():
            username = first.upper() + username[1:]

    # --- Validations basiques ---
    if not username:
        flash("Le pseudo est requis.")
        return redirect(url_for('register'))
    if not password:
        flash("Le mot de passe est requis.")
        return redirect(url_for('register'))
    try:
        validate_email(email_raw)
    except EmailNotValidError:
        flash('Email invalide.')
        return redirect(url_for('register'))

    # --- Unicité (email insensible à la casse + pseudo exact normalisé) ---
    existing = User.query.filter(
        or_(func.lower(User.email) == email, User.username == username)
    ).first()
    if existing:
        if existing.email and existing.email.lower() == email:
            flash("Cette adresse email est déjà utilisée.")
        elif existing.username == username:
            flash("Ce pseudo est déjà pris.")
        else:
            flash("Impossible de créer le compte avec ces informations.")
        return redirect(url_for('register'))

    # --- Création ---
    u = User(username=username, email=email, pw_hash=generate_password_hash(password), bolts=10)
    db.session.add(u)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        flash("Cette adresse email est déjà utilisée.")
        return redirect(url_for('register'))

    login_user(u)
    flash("Compte créé.")
    return redirect(url_for('initiale_page', fresh=1))

@app.route('/login', methods=['GET','POST'])
def login():
    if request.method == 'GET':
        body = """
        <form method=post>
          <label>Email</label><input type=email name=email required>
          <label>Mot de passe</label><input type=password name=password required>
          <div style='margin-top:12px;'><button class='btn primary'>Se connecter</button></div>
        </form>"""
        return render(AUTH_HTML, css=BASE_CSS, title='Se connecter', body=body)
    email = (request.form.get('email') or '').strip().lower()
    pw = (request.form.get('password') or '').strip()
    u = User.query.filter_by(email=email).first()
    if not u or not check_password_hash(u.pw_hash, pw):
        flash('Identifiants invalides.')
        return redirect(url_for('login'))
    login_user(u)
    return redirect(url_for('index'))

@app.route('/logout')
@login_required
def logout():
    logout_user(); return redirect(url_for('index'))

from flask_login import login_required, logout_user, current_user
from sqlalchemy import text
import os

@app.post("/account/delete")
@login_required
def delete_account():
    uid = current_user.id  # capture avant logout

    # 1) Déconnexion d’abord
    try:
        logout_user()
    except Exception:
        pass

    # 2) Supprime l’avatar disque (best-effort)
    try:
        avatar_path = os.path.join(app.static_folder, "avatars", f"{uid}.png")
        if os.path.exists(avatar_path):
            os.remove(avatar_path)
    except Exception:
        pass

    # 3) Purge base dans une transaction
    try:
        db.session.rollback()  # nettoie une éventuelle transaction en cours
    except Exception:
        pass

    try:
        # -- Tables qui ont un FK vers user(id) (d’après ton dump)
        child_tables_fk_user = [
            "position",
            "weather_position",
            "ppp_bet",
            "wet_bets",
            "ppp_boosts",
            "user_station",
            "art_bets",
        ]

        for t in child_tables_fk_user:
            db.session.execute(text(f"DELETE FROM {t} WHERE user_id = :uid"), {"uid": uid})

        # -- Autres tables optionnelles (sans FK) si présentes (best-effort)
        optional_tables = {
            # table : clause
            "bet_listing": "user_id = :uid",  # ta table Trade (si elle existe ici)
            "chat_messages": "from_user_id = :uid OR to_user_id = :uid",
            # ajoute ici d’autres tables non-FK si besoin
        }
        for t, clause in optional_tables.items():
            try:
                db.session.execute(text(f"DELETE FROM {t} WHERE {clause}"), {"uid": uid})
            except Exception:
                # on ignore si la table n'existe pas dans ce déploiement
                pass

        # -- Enfin, l’utilisateur
        u = db.session.get(User, uid)
        if u:
            db.session.delete(u)

        db.session.commit()

    except Exception:
        db.session.rollback()
        app.logger.exception("Erreur suppression compte %s", uid)
        flash("Suppression impossible pour le moment. Réessaie dans un instant.", "error")
        return redirect(url_for("ppp"))

    flash("Votre compte a été supprimé.", "success")
    return redirect(url_for("login"))

# -----------------------------------------------------------------------------
# Allocation initiale + création de positions avec échéance
# -----------------------------------------------------------------------------
MIN_WEEKS = 3
MAX_MONTHS = 6

def clamp_maturity(start: date, weeks: int) -> date:
    weeks = max(MIN_WEEKS, min(weeks, MAX_MONTHS*4))
    return start + timedelta(weeks=weeks)

@app.route('/allocate', methods=['GET','POST'])
@login_required
def allocate():
    flash("La section « Attribuer (initial) » a été retirée. Utilisez « Pluie Pas Pluie ».")
    return redirect(url_for('ppp'))

# -----------------------------------------------------------------------------
# Remiser (créer de nouvelles positions depuis les soldes libres)
# -----------------------------------------------------------------------------
@app.route('/stake', methods=['GET','POST'])
@login_required
def stake():
    flash("La section « Remiser » a été retirée. Utilisez « Pluie Pas Pluie ».")
    return redirect(url_for('ppp'))

from datetime import datetime, timedelta
from flask_login import login_required, current_user

def paris_now():
    # If you already have a tz-aware helper, use it. Otherwise:
    # We approximate by taking server now and formatting as Europe/Paris by API/JS elsewhere.
    # For slot validation we only need date+hour granularity.
    return datetime.now(timezone.utc)  # keep simple; your JS aligns display; adjust if you have tzinfo helper

def paris_floor_to_hour(dt: datetime) -> datetime:
    return dt.replace(minute=0, second=0, microsecond=0)

def station_by_id(sid):
    for s in load_stations():
        if s["id"] == sid:
            return s
    return None

@app.route('/ppp', methods=['GET','POST'])
@app.route('/ppp/<station_id>', methods=['GET','POST'])
@login_required
def ppp(station_id=None):
    # --------- contexte (ville & scope station) ----------
    if station_id:
        S = station_by_id(station_id)
        if not S:
            flash("Station introuvable.")
            return redirect(url_for('carte'))
        city_label = f"{S['city']}, France"
        scope_station_id = S["id"]
        page_title = f"{S['city']} — Pluie ou Pas Pluie"
    else:
        city_label = "Paris, France"
        scope_station_id = None
        page_title = "Zeus"

    def _ppp_url():
        return url_for('ppp', station_id=scope_station_id) if scope_station_id else url_for('ppp')

    # ------------------ POST: créer une mise ------------------
    if request.method == 'POST':
        try:
            target_str = (request.form.get('date') or '').strip()
            choice     = (request.form.get('choice') or '').strip().upper()
            amount     = round(float(request.form.get('amount') or 0), 2)
            # heure ciblée HH:MM (ex: "18:00"), défaut 18:00 si vide
            raw_hhmm   = (request.form.get('target_time') or '').strip() or '18:00'
        except Exception:
            flash("Entrées invalides.")
            return redirect(_ppp_url())

        if not target_str:
            flash("Cliquez sur une case du calendrier pour choisir la date.")
            return redirect(_ppp_url())

        if choice not in ('PLUIE', 'PAS_PLUIE') or amount <= 0:
            flash("Choix ou montant invalides.")
            return redirect(_ppp_url())

        # parse date choisie
        try:
            y, m, d = [int(x) for x in target_str.split('-')]
            target = date(y, m, d)
        except Exception:
            flash("Date invalide.")
            return redirect(_ppp_url())

        # clamp/valide l'heure HH:MM
        def clamp_hhmm(s: str) -> str:
            try:
                parts = str(s).split(':')
                h = max(0, min(23, int(parts[0])))
                m = max(0, min(59, int(parts[1]) if len(parts) > 1 else 0))
                return f"{h:02d}:{m:02d}"
            except Exception:
                return "18:00"

        hhmm = clamp_hhmm(raw_hhmm)

        # validations métier (J+3..J+31)
        today = today_paris_date()
        ok, msg, offset, odds = ppp_validate_can_bet(target, today)
        if not ok:
            flash(msg or "Mise impossible pour ce jour.")
            return redirect(_ppp_url())

        # empêcher paris contradictoires le même jour pour le même scope
        existing = (
            PPPBet.query
            .filter(
                PPPBet.user_id == current_user.id,
                PPPBet.bet_date == target,
                PPPBet.status == 'ACTIVE',
                PPPBet.station_id == scope_station_id
            )
            .order_by(PPPBet.id.asc())
            .all()
        )
        if existing and existing[0].choice != choice:
            flash(f"Vous avez déjà misé « {existing[0].choice.replace('_',' ')} » pour ce jour.")
            return redirect(_ppp_url())

        # budget
        grem = remaining_points(current_user)
        if amount > grem + 1e-6:
            flash(f"Budget insuffisant. Points restants : {grem:.3f}.")
            return redirect(_ppp_url())

        # insertion (inclut target_time)
        bet = PPPBet(
            user_id=current_user.id,
            bet_date=target,
            choice=choice,
            amount=amount,
            odds=float(odds),         # cote initiale (sans boost)
            status='ACTIVE',
            station_id=scope_station_id,
            funded_from_balance=1
        )
        # colonne ajoutée par migration idempotente
        try:
            setattr(bet, "target_time", hhmm)
        except Exception:
            pass

        db.session.add(bet)
        db.session.commit()

        flash(f"Mise enregistrée pour le {target.isoformat()} à {hhmm} — {choice.replace('_',' ')} à x{odds:.1f}.")

        # support AJAX si tu l'utilises
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({"ok": True, "target_time": hhmm})

        return redirect(url_for('you_bet', back=_ppp_url()))

    # ------------------ GET: afficher la page ------------------
    from datetime import timedelta

    solde_str = format_points_fr(remaining_points(current_user))
    bets_map: dict[str, dict] = {}

    # borne temps (pour inclure J-3 même si status != ACTIVE)
    today = today_paris_date()
    past3 = today - timedelta(days=3)

    # FUTUR (>= aujourd’hui) : seulement ACTIVE & non verrouillées
    rows_future_q = PPPBet.query.filter(
        PPPBet.user_id == current_user.id,
        PPPBet.station_id == scope_station_id,
        PPPBet.bet_date >= today,
        PPPBet.status == 'ACTIVE',
    )
    if hasattr(PPPBet, "locked_for_trade"):
        rows_future_q = rows_future_q.filter(PPPBet.locked_for_trade == False)
    rows_future = rows_future_q.all()

    # PASSÉ RÉCENT (J-3 .. J-1) : toutes statuses (pour afficher verdict)
    rows_past = (
        PPPBet.query
        .filter(
            PPPBet.user_id == current_user.id,
            PPPBet.station_id == scope_station_id,
            PPPBet.bet_date >= past3,
            PPPBet.bet_date < today,
        )
        .all()
    )

    # Fusion triée (date + id)
    rows = sorted(rows_future + rows_past, key=lambda r: (r.bet_date, r.id))

    for r in rows:
        key = r.bet_date.isoformat()
        entry = bets_map.get(key, {"amount": 0.0, "choice": r.choice, "bets": []})

        entry["amount"] += float(r.amount or 0.0)
        if r.choice:
            entry["choice"] = r.choice

        try:
            when_iso = (r.created_at.isoformat() if getattr(r, "created_at", None) else key + "T00:00:00")
        except Exception:
            when_iso = key + "T00:00:00"

        # ---- Déduction du verdict si manquant ----
        v = (getattr(r, "verdict", None) or getattr(r, "result", None))
        if not v:
            try:
                outcome = (getattr(r, "outcome", None) or "").upper()
                choice  = (getattr(r, "choice",  None) or "").upper()
                if outcome and choice:
                    v = "WIN" if outcome == choice else "LOSE"
            except Exception:
                v = None

        bet_dict = {
            "when": when_iso,
            "amount": float(r.amount or 0.0),
            "odds": float(r.odds or 1.0),
            "target_time": getattr(r, "target_time", None) or "18:00",
            "verdict": v,
            "result": v,  # compatibilité anciens templates
            "outcome": getattr(r, "outcome", None),
            "observed_mm": (
                float(getattr(r, "observed_mm", 0.0))
                if getattr(r, "observed_mm", None) not in (None, "")
                else None
            ),
        }
        entry["bets"].append(bet_dict)

        # 🎯 verdict agrégé du jour (priorité au rouge)
        results_upper = [str((b.get("verdict") or b.get("result") or "")).upper() for b in entry["bets"]]
        if "LOSE" in results_upper:
            entry["verdict"] = "LOSE"
        elif "WIN" in results_upper:
            entry["verdict"] = "WIN"
        else:
            entry["verdict"] = None

        bets_map[key] = entry

    # -------- Boosts groupés par jour pour CE scope (normalisation station_id -> '') --------
    boosts_map = {}
    sid_norm = scope_station_id or ""  # IMPORTANT: même normalisation que dans /ppp/boost
    res = db.session.execute(
        text("""
          SELECT bet_date AS d, SUM(COALESCE(value,0)) AS total
            FROM ppp_boosts
           WHERE user_id = :uid
             AND COALESCE(station_id, '') = :sid
           GROUP BY d
        """),
        {"uid": current_user.id, "sid": sid_norm}
    )
    for d, total in res:
        key = d.isoformat() if hasattr(d, "isoformat") else str(d)[:10]
        boosts_map[key] = float(total or 0.0)

    return render_template_string(
        PPP_HTML.replace("Zeus", page_title),
        css=BASE_CSS,
        solde_str=solde_str,
        bets_map=bets_map,
        boosts_map=boosts_map,
        city_label=city_label,
        station_id=scope_station_id  # pour que le JS puisse l'envoyer lors des boosts
    )

@app.route("/api/chat/unread_count")
@login_required
def api_chat_unread_count():
    count = ChatMessage.query.filter_by(
        to_user_id=current_user.id,
        is_read=0
    ).count()
    return jsonify({"unread": int(count)})

@app.route("/api/chat/mark_all_read", methods=["POST"])
@login_required
def api_chat_mark_all_read():
    ChatMessage.query.filter_by(
        to_user_id=current_user.id,
        is_read=0
    ).update({"is_read": 1})
    db.session.commit()
    return jsonify({"ok": True})

# --- Route /api/users/me : renvoie le solde et le pseudo de l'utilisateur connecté ---
from flask_login import login_required, current_user
from flask import jsonify

@app.get("/api/users/me")
@login_required
def api_user_me():
    """Renvoie les infos essentielles de l'utilisateur connecté (solde, pseudo)."""
    try:
        pts = user_solde(current_user)
        return jsonify({
            "id": current_user.id,
            "username": current_user.username,
            "points": pts,
        }), 200
    except Exception as e:
        app.logger.exception("Erreur /api/users/me : %s", e)
        return jsonify({"error": str(e)}), 500

# --- WET: 48h humidity betting ----------------------------------------------
@app.route('/wet', methods=['GET', 'POST'])
@login_required
def wet():
    # ---------- POST: enregistrement d'une mise ----------
    if request.method == 'POST':
        try:
            slot_iso   = (request.form.get('slot') or '').strip()
            target_pct = int(request.form.get('target') or 0)
            amount     = float(request.form.get('amount') or 0)
        except Exception:
            flash("Entrées invalides.")
            return redirect(url_for('wet'))

        if not slot_iso:
            flash("Heure manquante.")
            return redirect(url_for('wet'))
        if target_pct < 0 or target_pct > 100 or amount <= 0:
            flash("Valeurs invalides.")
            return redirect(url_for('wet'))

        # Parse
        try:
            slot_dt = datetime.fromisoformat(slot_iso)  # naive local hour as per your model
        except Exception:
            flash("Créneau invalide.")
            return redirect(url_for('wet'))

        # Paris cutoff: current hour (floor) + 2h
        now_paris = paris_now()
        hour0     = now_paris.replace(minute=0, second=0, microsecond=0)
        cutoff    = hour0 + timedelta(hours=2)
        if slot_dt < cutoff.replace(tzinfo=None):
            flash(f"Vous pouvez miser à partir de {cutoff.strftime('%Hh')} (heure de Paris).")
            return redirect(url_for('wet'))

        # Budget
        u   = db.session.get(User, current_user.id)
        rem = remaining_points(u)
        if amount > rem + 1e-9:
            flash(f"Budget insuffisant. Points restants : {rem:.2f}")
            return redirect(url_for('wet'))

        # Interdire de changer la cible si une mise existe déjà pour ce slot
        existing = WetBet.query.filter_by(
            user_id=current_user.id,
            slot_dt=slot_dt,
            status='ACTIVE'
        ).first()
        if existing and abs(existing.target_pct - target_pct) > 1e-6:
            flash(
                f"Vous avez déjà misé sur {existing.target_pct:.0f}% pour ce créneau. "
                "Vous pouvez ajouter du montant sur la même cible, mais pas la changer."
            )
            return redirect(url_for('wet'))

        # Guard against past (keep the same naive-Paris style as slot_dt)
        now_paris_aware = datetime.now(ZoneInfo("Europe/Paris"))
        now_paris_naive = now_paris_aware.replace(tzinfo=None)

        if slot_dt <= now_paris_naive:
            flash("Impossible de miser sur une heure passée.")
            return redirect(url_for('wet'))

        # Cote simple: augmente légèrement avec l’horizon (1.2 .. 3.0)
        hours_ahead = max(0.0, (slot_dt - now_paris_naive).total_seconds() / 3600.0)
        odds = max(1.2, min(3.0, 1.2 + 0.02 * hours_ahead))

        # Enregistrer
        db.session.add(WetBet(
            user_id=current_user.id,
            slot_dt=slot_dt,
            target_pct=target_pct,
            amount=round(amount, 6),
            odds=float(odds),
            status='ACTIVE'
        ))
        db.session.commit()

        flash(f"Mise Wet enregistrée — {slot_iso}, cible {target_pct}%, {amount:.2f} pts (x{odds:.1f}).")

        # AJAX short-circuit
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({"ok": True})

        # Redirect to "You Bet" splash, then back to Wet
        return redirect(url_for('you_bet', back=url_for('wet')))

    # ---------- GET ----------
    # Safely try to resolve due bets (don’t break the page if resolver fails)
    try:
        resolve_due_wet_bets(current_user)
    except Exception as e:
        app.logger.warning("resolve_due_wet_bets skipped: %s", e)

    solde_str = format_points_fr(remaining_points(current_user))

    # Build future 48h slots (Paris hours)
    now_paris = paris_now()
    hour0     = now_paris.replace(minute=0, second=0, microsecond=0)
    cutoff    = hour0 + timedelta(hours=2)

    slots = []
    for i in range(48):
        dt = hour0 + timedelta(hours=i)
        slots.append({
            "iso": dt.replace(tzinfo=None).isoformat(),
            "label": dt.strftime("%a %d %Hh"),
            "odds": 2.0,
        })

    # Past/resolved tiles (last 28h, not dismissed)
    cutoff_resolved = datetime.now(timezone.utc) - timedelta(hours=28)
    resolved = (WetBet.query
        .filter(WetBet.user_id == current_user.id,
                WetBet.status == 'RESOLVED',
                WetBet.slot_dt >= cutoff_resolved,
                WetBet.dismissed_at.is_(None))
        .all())

    past_tiles = []
    for r in resolved:
        if r.outcome == 'EXACT':
            cls = 'past-exact'
        elif r.outcome == 'WIN':
            cls = 'past-win'
        else:
            cls = 'past-lose'
        past_tiles.append({
            "iso": r.slot_dt.isoformat(),
            "label": r.slot_dt.strftime("%a %d %Hh"),
            "odds": float(r.odds or 1.0),
            "amount": float(r.amount or 0.0),
            "target": int(r.target_pct or 0),
            "observed": (int(r.observed_pct) if r.observed_pct is not None else None),
            "payout": float(r.payout or 0.0),
            "klass": cls,
        })

    # Active stakes map for display
    rows = (WetBet.query
        .filter(WetBet.user_id == current_user.id, WetBet.status == 'ACTIVE')
        .with_entities(WetBet.slot_dt, WetBet.target_pct, db.func.sum(WetBet.amount))
        .group_by(WetBet.slot_dt, WetBet.target_pct)
        .all())

    bets_map = {}
    for slot_dt, target_pct, total_amt in rows:
        key = slot_dt.isoformat()
        bets_map[key] = {"target": float(target_pct or 0.0), "amount": float(total_amt or 0.0)}

    try:
        today_utc0 = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        has_today = (HumidityObservation.query
                     .filter(HumidityObservation.station_id == "cdg_07157",
                             HumidityObservation.obs_time >= today_utc0)
                     .first() is not None)
        if not has_today:
            app.logger.info("No obs for today -> ingest Infoclimat CDG")
            ingest_infoclimat_cdg("cdg_07157")
    except Exception as e:
        app.logger.warning("Auto-ingest skipped: %s", e)    

    # Build obs_data from HumidityObservation for the slots and current station
    current_station_id = request.args.get("station_id") or getattr(current_user, "default_station_id", None) or "cdg_07157"
    obs_data = {}
    try:
        tz_paris = ZoneInfo("Europe/Paris")
        for s in slots:
            slot_iso = s["iso"]
            slot_dt = dtparse.parse(slot_iso)
            if slot_dt.tzinfo is None:
                slot_dt = slot_dt.replace(tzinfo=tz_paris)
            slot_utc = slot_dt.astimezone(timezone.utc)
            obs = (HumidityObservation.query
                   .filter_by(station_id=current_station_id)
                   .filter(HumidityObservation.obs_time >= slot_utc)
                   .order_by(HumidityObservation.obs_time.asc())
                   .first())
            if obs:
                obs_data[slot_iso] = {"humidity": float(obs.humidity), "obs_time": obs.obs_time.isoformat()}
    except Exception as e:
        app.logger.warning("obs_data build skipped: %s", e)
        obs_data = {}

    return render_template_string(
        WET_HTML,
        css=BASE_CSS,
        solde_str=solde_str,
        slots=slots,
        bets_map=bets_map,
        past_tiles=past_tiles,
        obs_data=obs_data,
        today_str=date.today().isoformat(),
        current_station_id=(request.args.get('station_id') or getattr(current_user, 'default_station_id', None) or 'cdg_07157'),   # station courante
    )

@app.route('/wet/dismiss', methods=['POST'])
@login_required
def wet_dismiss():
    iso = (request.form.get('slot') or '').strip()
    if not iso:
        return jsonify({"ok": False, "err": "slot missing"}), 400
    try:
        dt = datetime.fromisoformat(iso)
    except Exception:
        return jsonify({"ok": False, "err": "bad iso"}), 400

    b = (WetBet.query
         .filter_by(user_id=current_user.id, slot_dt=dt)
         .first())
    if not b:
        return jsonify({"ok": False, "err": "not found"}), 404

    b.dismissed_at = datetime.now(timezone.utc)
    db.session.commit()
    return jsonify({"ok": True})

@app.delete("/api/my_stations/<sid>")
@login_required
def delete_my_station(sid):
    """
    Retire l'association user->station (📅) et RÉINITIALISE le calendrier
    de cette station pour cet utilisateur (supprime ses mises & boosts).
    On NE supprime PAS la station du catalogue.
    """
    uid = current_user.id
    try:
        # 1) Enlever l'association (le drapeau n’apparaîtra plus)
        db.session.execute(
            text("DELETE FROM user_station WHERE user_id = :uid AND station_id = :sid"),
            {"uid": uid, "sid": sid}
        )
        # 2) Vider le calendrier de CET utilisateur sur CETTE station
        db.session.execute(
            text("DELETE FROM ppp_bet WHERE user_id = :uid AND station_id = :sid"),
            {"uid": uid, "sid": sid}
        )
        db.session.execute(
            text("DELETE FROM ppp_boosts WHERE user_id = :uid AND station_id = :sid"),
            {"uid": uid, "sid": sid}
        )
        db.session.commit()
        return jsonify(ok=True)
    except Exception as e:
        db.session.rollback()
        return jsonify(ok=False, error=str(e)), 500   

# -----------------------------------------------------------------------------
# Admin (identique à v1 mais sans publication instantanée pour concision)
# -----------------------------------------------------------------------------
from flask_login import current_user

def require_admin():
    if not current_user.is_authenticated or not current_user.is_admin:
        abort(403)

@app.route('/admin', methods=['GET','POST'])
@login_required
def admin():
    require_admin()
    if request.method == 'POST':
        try:
            the_date = datetime.strptime(request.form.get('the_date'), '%Y-%m-%d').date()
            pv = float(request.form.get('pierre_value'))
            mv = float(request.form.get('marie_value'))
        except Exception:
            flash('Entrées invalides.')
            return redirect(url_for('admin'))
        row = PendingMood.query.filter_by(the_date=the_date).first()
        if not row:
            db.session.add(PendingMood(the_date=the_date, pierre_value=pv, marie_value=mv))
        else:
            row.pierre_value, row.marie_value = pv, mv
        db.session.commit(); flash('Valeurs enregistrées.')
        return redirect(url_for('admin'))
    published = DailyMood.query.order_by(DailyMood.the_date.desc()).limit(60).all()
    return render(ADMIN_HTML, css=BASE_CSS, published=published)

from datetime import date, datetime
try:
    from datetime import UTC
except ImportError:
    from datetime import timezone as _tz
    UTC = _tz.utc

from flask import request, jsonify, current_app
from flask_login import login_required, current_user
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

@app.post("/ppp/boost")
@login_required
def ppp_boost():
    """
    Ajoute un boost (éclair) de +value (par défaut +5.0) à la cote d'une date,
    éventuellement pour une station. Consomme 1 éclair (User.bolts) SI ET SEULEMENT SI
    on peut encore augmenter la valeur (plafond non atteint).

    Plafond total clampé par cible (user_id, bet_date, station_id_normalisé) :
      total <= MAX_BOOSTS_PER_TARGET * BOOST_UNIT
    Réponse JSON: { ok, total, bolts_left } (+ erreurs explicites)
    """
    data = request.get_json(silent=True) or {}
    date_str   = (data.get("date") or "").strip()
    station_id = data.get("station_id")

    # --- Config plafond ---
    MAX_BOOSTS_PER_TARGET = int(getattr(app.config, "PPP_MAX_BOOSTS_PER_TARGET", 5))
    BOOST_UNIT            = float(getattr(app.config, "PPP_BOOST_UNIT", 5.0))
    cap_value             = MAX_BOOSTS_PER_TARGET * BOOST_UNIT

    # Normalisation: chaîne vide pour « toutes stations »
    sid_norm = (station_id or "")

    # --- Parse inputs ---
    try:
        inc_req = float(data.get("value") or BOOST_UNIT)
    except Exception:
        return jsonify(ok=False, error="bad_value"), 400
    if inc_req <= 0:
        return jsonify(ok=False, error="bad_value"), 400

    if not date_str:
        return jsonify(ok=False, error="missing_date"), 400
    try:
        bet_date = date.fromisoformat(date_str)
    except Exception:
        return jsonify(ok=False, error="bad_date"), 400

    uid = int(current_user.id)

    # --- SQL helpers ---
    sel_total_sql = text("""
        SELECT COALESCE(SUM(COALESCE(value, 0.0)), 0.0)
          FROM ppp_boosts
         WHERE user_id = :uid
           AND bet_date = :d
           AND COALESCE(station_id, '') = :sid
    """)

    upd_bolts_sql = text("""
        UPDATE user
           SET bolts = bolts - 1
         WHERE id = :uid
           AND COALESCE(bolts, 0) > 0
    """)

    upsert_sql = text("""
        INSERT INTO ppp_boosts (user_id, bet_date, station_id, value, created_at)
        VALUES (:uid, :d, :sid, MIN(:inc, :cap), :now)
        ON CONFLICT(user_id, bet_date, station_id)
        DO UPDATE SET
            value = MIN(COALESCE(ppp_boosts.value, 0) + :inc, :cap)
    """)

    try:
        # 1) Lire le total actuel (somme robuste)
        cur_total = float(
            db.session.execute(
                sel_total_sql, {"uid": uid, "d": bet_date, "sid": sid_norm}
            ).scalar() or 0.0
        )

        # 2) Vérifier marge restante
        remaining = cap_value - cur_total
        if remaining <= 1e-12:
            # déjà au plafond → ne pas consommer d'éclair
            u = db.session.get(User, uid)
            return jsonify(ok=False, error="cap_reached", total=cur_total,
                           bolts_left=int((u and u.bolts) or 0)), 400

        # 3) Clamp de l'incrément demandé
        inc = min(inc_req, remaining)
        if inc <= 1e-12:
            u = db.session.get(User, uid)
            return jsonify(ok=False, error="cap_reached", total=cur_total,
                           bolts_left=int((u and u.bolts) or 0)), 400

        # 4) Décrémenter 1 éclair (si dispo)
        res = db.session.execute(upd_bolts_sql, {"uid": uid})
        if res.rowcount != 1:
            db.session.rollback()
            u = db.session.get(User, uid)
            return jsonify(ok=False, error="no_bolts",
                           bolts_left=int((u and u.bolts) or 0)), 400

        # 5) UPSERT clampé
        db.session.execute(upsert_sql, {
            "uid": uid, "d": bet_date, "sid": sid_norm,
            "inc": inc, "cap": cap_value, "now": datetime.now(UTC),
        })

        # 6) Lire le total après mise à jour + stock restant
        new_total = float(
            db.session.execute(
                sel_total_sql, {"uid": uid, "d": bet_date, "sid": sid_norm}
            ).scalar() or 0.0
        )
        bolts_left = int((db.session.get(User, uid) or User()).bolts or 0)

        # 7) Commit la transaction implicite
        db.session.commit()
        return jsonify(ok=True, total=new_total, bolts_left=bolts_left), 200

    except IntegrityError:
        db.session.rollback()
        return jsonify(ok=False, error="conflict"), 409
    except Exception:
        db.session.rollback()
        current_app.logger.exception("ppp_boost server_error")
        return jsonify(ok=False, error="server_error"), 500

@app.post('/api/ppp/bets/<int:bet_id>/boosts')
@login_required
def ppp_update_boosts(bet_id):
    me = int(current_user.get_id())
    bet = PPPBet.query.get_or_404(bet_id)
    if int(bet.user_id) != me:
        return jsonify({"ok": False, "error": "forbidden"}), 403

    data = request.get_json(silent=True) or {}
    boosts_count = int(data.get('boosts_count') or 0)
    boosts_add   = float(data.get('boosts_add') or 0.0)

    # colonnes directes si elles existent
    if hasattr(bet, 'boosts_count'): bet.boosts_count = boosts_count
    if hasattr(bet, 'boosts_add'):   bet.boosts_add   = boosts_add

    # garde aussi dans payload (tes UIs lisent souvent depuis payload)
    pl = dict(bet.payload or {})
    pl['boosts_count'] = boosts_count
    pl['boosts_add']   = boosts_add
    # recalcul optionnel
    base_odds = float(pl.get('base_odds') or getattr(bet, 'odds', 1.0) or 1.0)
    pl['total_odds'] = base_odds + boosts_add
    bet.payload = pl

    db.session.commit()
    return jsonify({"ok": True})

@app.get("/carte")
@login_required
def carte():
    solde_str = format_points_fr(remaining_points(current_user)) if current_user.is_authenticated else None
    return render_template_string(CARTE_HTML, css=BASE_CSS, solde_str=solde_str)

@app.get("/api/stations")
@login_required
def api_stations():
    q = (request.args.get("q") or "").strip().lower()
    stations = load_stations()
    if q:
        def match(s):
            return (
                q in (s.get("city","") or "").lower()
                or q == (s.get("dept","") or "").lower()
                or q in (s.get("name","") or "").lower()
                or q == (s.get("icao","") or "").lower()
            )
        stations = [s for s in stations if match(s)]
    # ne renvoie que ce qu’il faut côté front
    out = []
    for s in stations:
        out.append({
            "id": s["id"], "name": s["name"], "city": s["city"], "dept": s["dept"],
            "icao": s["icao"], "lat": s["lat"], "lon": s["lon"], "label": s["label"]
        })
    return jsonify({"stations": out})

@app.post("/api/my_stations")
@login_required
def add_my_station():
    data = request.get_json(force=True)  # {id,label,lat,lon}
    sid   = (data.get("id") or "").strip()
    label = (data.get("label") or "").strip()
    lat   = data.get("lat")
    lon   = data.get("lon")
    if not sid or not label:
        return jsonify(ok=False, error="missing id/label"), 400

    # upsert simple sur user_station
    try:
        db.session.execute(
            text("""
                INSERT INTO user_station (user_id, station_id, station_label, lat, lon)
                VALUES (:uid, :sid, :lbl, :lat, :lon)
                ON CONFLICT(user_id, station_id) DO UPDATE SET
                    station_label = excluded.station_label,
                    lat = excluded.lat,
                    lon = excluded.lon
            """),
            {"uid": current_user.id, "sid": sid, "lbl": label, "lat": lat, "lon": lon}
        )
        db.session.commit()
        return jsonify(ok=True)
    except Exception as e:
        db.session.rollback()
        return jsonify(ok=False, error=str(e)), 500

@app.get("/api/my_stations")
@login_required
def list_my_stations():
    rows = db.session.execute(
        text("""
            SELECT station_id AS id, station_label AS label, lat, lon
            FROM user_station
            WHERE user_id = :uid
            ORDER BY station_label ASC
        """),
        {"uid": current_user.id}
    ).mappings().all()
    return jsonify(stations=[dict(r) for r in rows])

from dateutil import parser as dtparse
from datetime import timezone, timedelta

@app.route('/api/wet/observations', methods=['GET'])
@login_required
def wet_observations():
    station_id = (request.args.get('station_id') or '').strip()
    slots = request.args.getlist('slot')
    if not station_id or not slots:
        return jsonify({}), 200

    tz_paris = ZoneInfo('Europe/Paris')
    out = {}

    for slot_iso in slots:
        try:
            # 1) Interpréter le slot comme heure de Paris (si naïf)
            slot_local = dtparse.parse(slot_iso)
            if slot_local.tzinfo is None:
                slot_local = slot_local.replace(tzinfo=tz_paris)
            start_utc = slot_local.astimezone(timezone.utc)
            end_utc   = start_utc + timedelta(minutes=59, seconds=59)

            # 2) D'abord: première obs DANS la fenêtre [start, end] (asc)
            qwin = (HumidityObservation.query
                    .filter_by(station_id=station_id)
                    .filter(HumidityObservation.obs_time >= start_utc,
                            HumidityObservation.obs_time <= end_utc)
                    .order_by(HumidityObservation.obs_time.asc()))
            obs = qwin.first()

            # 3) Fallback: si rien, dernière obs <= end (desc)
            if not obs:
                qfb = (HumidityObservation.query
                       .filter_by(station_id=station_id)
                       .filter(HumidityObservation.obs_time <= end_utc)
                       .order_by(HumidityObservation.obs_time.desc()))
                obs = qfb.first()

            out[slot_iso] = (
                {'humidity': float(obs.humidity), 'obs_time': obs.obs_time.isoformat()}
                if obs else None
            )
        except Exception as e:
            app.logger.warning('wet_observations slot=%s error=%s', slot_iso, e)
            out[slot_iso] = None

    return jsonify(out), 200

@app.get('/admin/wet/debug')
@login_required
def wet_debug():
    station = request.args.get('station_id', 'cdg_07157')
    last = (HumidityObservation.query
            .filter_by(station_id=station)
            .order_by(HumidityObservation.obs_time.desc())
            .first())
    count = (HumidityObservation.query
             .filter_by(station_id=station)
             .count())
    return jsonify({
        "station_id": station,
        "count": count,
        "latest_obs_time_utc": (last.obs_time.isoformat() if last else None),
        "latest_humidity": (float(last.humidity) if last else None),
    }), 200

import os
from flask import send_file

AVATAR_DIR = os.path.join(app.static_folder, "avatars")
os.makedirs(AVATAR_DIR, exist_ok=True)  # s'assure que le dossier existe

DEFAULT_AVATAR_PATH = os.path.join(app.static_folder, "img", "avatar_default.png")
if not os.path.exists(DEFAULT_AVATAR_PATH):
    # petit filet: utilise l'avatar de la cabine comme placeholder si tu n'as pas de default
    DEFAULT_AVATAR_PATH = os.path.join(app.static_folder, "cabine", "assets", "avatar.png")
    
# -----------------------------------------------------------------------------
# Init DB avec quelques données si vide
# -----------------------------------------------------------------------------
@app.cli.command('init-db')
def init_db_cmd():
  with app.app_context():
    with app.app_context():
        db.create_all()
    if DailyMood.query.count() == 0:
        base = today_paris() - timedelta(days=30)
        for i in range(0, 30):
            d = base + timedelta(days=i)
            db.session.add(DailyMood(the_date=d, pierre_value=100 + i*1.0, marie_value=120 + i*0.8, published_at=dt_paris_now()))
        db.session.commit()
        print('DB initialisée avec des données exemples.')
    else:
        print('DB déjà initialisée.')

# -----------------------------------------------------------------------------
# Run
# -----------------------------------------------------------------------------
@app.route('/_debug')
def _debug():
    return jsonify({
        "db_uri": app.config['SQLALCHEMY_DATABASE_URI'],
        "moods_count": DailyMood.query.count(),
        "today_has_value": DailyMood.query.filter_by(the_date=today_paris()).count() > 0
    })

from datetime import datetime, timezone, timedelta
from flask import g
from sqlalchemy import text

@app.before_request
def _touch_online():
    # Throttle: pas plus d'une MAJ toutes les 20s par session
    g._touch_done = False
    if not current_user.is_authenticated:
        return
    now = datetime.now(timezone.utc)
    last_touched = getattr(g, "_last_touch_ts", None)
    if last_touched and (now - last_touched) < timedelta(seconds=20):
        return
    try:
        tbl = User.__table__.name
        db.session.execute(
            text(f"UPDATE {tbl} SET last_seen = :ts WHERE id = :uid"),
            {"ts": now, "uid": current_user.id}
        )
        db.session.commit()
        g._last_touch_ts = now
        g._touch_done = True
    except Exception:
        db.session.rollback()

# --- 1b) Ping: met à jour last_seen ---
from datetime import datetime, timezone

@app.post('/api/users/ping')
@login_required
def api_users_ping():
    try:
        current_user.last_seen = datetime.now(timezone.utc)
        db.session.commit()
        return jsonify({"ok": True}), 200
    except Exception as e:
        app.logger.exception("users_ping failed")
        db.session.rollback()
        return jsonify({"ok": False, "error": str(e)}), 500        
   
# === CABINE INTEGRATION (auto) ===
import os
from sqlalchemy import inspect, exc
from sqlalchemy.sql.compiler import IdentifierPreparer

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CABINE_DIR = os.path.join(APP_DIR, "static", "cabine")

# --- Page Cabine ---
@app.route("/cabine/")
def cabine_page():
    # sert index.html depuis static/cabine/
    return send_from_directory(CABINE_DIR, "index.html")

@app.route("/cabine/<path:path>")
def cabine_assets(path):
    # sert CSS/JS/assets supplémentaires (au cas où)
    return send_from_directory(CABINE_DIR, path)

from flask_login import current_user

def current_user_id():
    try:
        if getattr(current_user, "is_authenticated", False):
            return str(current_user.get_id())
    except Exception:
        pass
    # visiteur non connecté → pas d'ID
    return None

# Table légère pour préférences Cabine
from sqlalchemy import Column, String, Text
try:
    # Si le modèle existe déjà, ne pas redéclarer
    AvatarPrefs = globals().get("AvatarPrefs")
    if AvatarPrefs is None:
        class AvatarPrefs(db.Model):
            __tablename__ = "avatar_prefs"
            user_id = Column(String(128), primary_key=True)
            data_json = Column(Text, nullable=False, default="{}")
        globals()["AvatarPrefs"] = AvatarPrefs
except Exception:
    pass

# Crée table si absente (idempotent)
with app.app_context():
    with app.app_context():
        db.create_all()

# --- tiny self-heal for bet_listing schema on SQLite ---
def ensure_bet_listing_columns():
    from sqlalchemy import text
    with db.engine.begin() as conn:
        cols = set()
        for row in conn.execute(text("PRAGMA table_info(bet_listing)")):
            cols.add(row[1])  # name

        # add missing columns (SQLite syntax)
        if "kind" not in cols:
            conn.execute(text("ALTER TABLE bet_listing ADD COLUMN kind VARCHAR(16)"))
            conn.execute(text("UPDATE bet_listing SET kind = 'PPP' WHERE kind IS NULL"))

        if "status" not in cols:
            conn.execute(text("ALTER TABLE bet_listing ADD COLUMN status VARCHAR(16)"))
            conn.execute(text("UPDATE bet_listing SET status = 'OPEN' WHERE status IS NULL"))

        if "expires_at" not in cols:
            conn.execute(text("ALTER TABLE bet_listing ADD COLUMN expires_at DATETIME"))

        if "payload" not in cols:
            # JSON sera stocké en TEXT sur SQLite
            conn.execute(text("ALTER TABLE bet_listing ADD COLUMN payload JSON"))
            conn.execute(text("UPDATE bet_listing SET payload = '{}' WHERE payload IS NULL"))        

# === Page Trade (front) ======================================================
from flask import render_template, url_for
from flask_login import login_required, current_user
import time  # <-- nécessaire
# (assure-toi aussi d'avoir: from datetime import datetime, timezone si besoin ailleurs)

# --- DESSIN (statique) ---
import pathlib, os
from flask import send_from_directory

APP_DIR = pathlib.Path(__file__).parent
DESSIN_DIR = APP_DIR / "static" / "dessin"

@app.route("/dessin/")
def dessin_page():
    return send_from_directory(os.fspath(DESSIN_DIR), "dessin.html")

@app.route("/static/dessin/dessin.html")
def redirect_old_dessin():
    return redirect(url_for("dessin_page"), code=301)

@app.route("/dessin/<path:path>")
def dessin_assets(path):
    return send_from_directory(os.fspath(DESSIN_DIR), path)

from time import time

@app.route("/trade/")
@login_required
def trade_page():
    uid = str(current_user.get_id())

    # URL de l’avatar via la route /u/<id>/avatar.png (pas le chemin static direct)
    avatar_url = url_for("user_avatar_png", user_id=uid) + f"?v={int(time())}"

    # Solde
    try:
        solde_txt = format_points_fr(remaining_points(current_user))
    except Exception:
        solde_txt = "0 pts"

    return render_template(
        "trade/index.html",
        me_avatar_url=avatar_url,   # <— on passe bien me_avatar_url
        solde_str=solde_txt
    )

# (optionnel) accepter /trade sans slash et rediriger vers /trade/
@app.route("/trade")
@login_required
def trade_page_noslash():
    return redirect(url_for("trade_page"))

from flask_login import login_required, current_user
from datetime import datetime, timezone, timedelta

# Utilitaire d'affichage du compte à rebours H-xx
def _fmt_countdown(dt_utc):
    if dt_utc.tzinfo is None:
        dt_utc = dt_utc.replace(tzinfo=timezone.utc)
    delta = dt_utc - datetime.now(timezone.utc)
    hours = int(delta.total_seconds() // 3600)
    return f"H-{hours if hours >= 0 else 0}"

def _fmt_label(b):
    """
    Construit la ligne type :
    "H-26 - Lundi 27 novembre - 12 pts (x1,4) - 2 ⚡ (x10) - 💧 GP: 134,4 pts"
    Ajuste selon tes champs de modèle.
    """
    h = _fmt_countdown(b.deadline_utc)             # b.deadline_utc = échéance UTC (datetime)
    jour = b.deadline_utc.strftime("%A %d %B")     # FR selon locale système
    mise = f"{b.stake:.2f}".rstrip('0').rstrip('.')  # b.stake = mise en points (float)
    cote = f"{b.odds:.2f}".replace('.', ',')       # b.odds = cote initiale (float)
    n_bolts = getattr(b, "boosts_count", 0)        # nombre d'éclairs éventuels
    mult_bolts = getattr(b, "boosts_multiplier", 1.0)  # multiplicateur cumulé
    symb = {"PLUIE":"💧","PAS_PLUIE":"☀️","NUAGES":"☁️"}.get(b.kind, b.kind or "❓")  # b.kind = PLUIE/SOLEIL/...
    gp = f"{b.potential_gain:.2f}".replace('.', ',')  # b.potential_gain = gains potentiels

    bolts_part = f" - {n_bolts} ⚡️(x{int(mult_bolts)})" if n_bolts else ""
    return f"{h} - {jour} - {mise} pts (x{cote}){bolts_part} - {symb} GP: {gp} pts"

from flask import jsonify
from flask_login import login_required, current_user
from sqlalchemy import text
from datetime import datetime

# -- petits helpers d’affichage, alignés PPP --
def fmt_fr(x, nd=2):
    try:
        x = float(x)
    except Exception:
        return "0"
    s = f"{x:.{nd}f}".rstrip('0').rstrip('.')
    return s.replace('.', ',')

def _fmt_date_fr_daykey(daykey: str):
    try:
        dt = datetime.strptime(daykey, "%Y-%m-%d")
        mois  = ["janvier","février","mars","avril","mai","juin",
                 "juillet","août","septembre","octobre","novembre","décembre"][dt.month-1]
        jours = ["lundi","mardi","mercredi","jeudi","vendredi","samedi","dimanche"][dt.weekday()]
        return f"{jours.capitalize()} {dt.day} {mois}"
    except Exception:
        return daykey or "—"

def _station_label(station_id):
    if station_id:
        s = station_by_id(station_id)
        if s:  # format ex: "CDG — Paris"
            return s.get("city") or s.get("name") or str(station_id)
    return "Paris"

from flask import request, jsonify
from flask_login import login_required, current_user

# --- outils simples ---
def _clip_text(s: str, max_len=2000) -> str:
    s = (s or "").strip()
    if len(s) > max_len:
        s = s[:max_len]
    return s

import re
from sqlalchemy import text

GIFT_RE = re.compile(r'^(toyou|tome)\s*🎁\s*([0-9]+(?:[.,][0-9]+)?)\s*$', re.IGNORECASE)

def _ensure_bonus_points_column():
    """Ajoute user.bonus_points si absent (SQLite safe). Appelé au boot."""
    try:
        res = db.session.execute(text("PRAGMA table_info(user)")).fetchall()
        cols = {r[1] for r in res}
        if "bonus_points" not in cols:
            db.session.execute(text("ALTER TABLE user ADD COLUMN bonus_points FLOAT DEFAULT 0.0"))
            db.session.commit()
    except Exception:
        db.session.rollback()

def credit_bonus_points(user_id: int, amount: float, reason: str = "gift"):
    """Crédite le champ user.bonus_points (ledger minimal)."""
    if amount <= 0:
        return
    # UPDATE direct pour éviter races (moins d’objets en mémoire)
    db.session.execute(
        text("UPDATE user SET bonus_points = COALESCE(bonus_points, 0) + :amt WHERE id = :uid"),
        {"amt": float(amount), "uid": int(user_id)}
    )

def parse_gift(body: str):
    """Retourne ('toyou'|'tome', amount) ou None si pas un cadeau."""
    if not body:
        return None
    m = GIFT_RE.match(body.strip())
    if not m:
        return None
    kind = m.group(1).lower()
    amt_str = m.group(2).replace(',', '.')
    try:
        amt = float(amt_str)
    except Exception:
        return None
    return (kind, amt)

def process_gift_if_any(body: str, from_uid: int, to_uid: int):
    """
    Si body est un code cadeau, applique le crédit et renvoie le body final à stocker.
    - toyou🎁N  -> crédite to_uid, body affiché = '🎁N'
    - tome🎁N    -> crédite from_uid, body affiché = 'tome🎁N'
    """
    parsed = parse_gift(body)
    if not parsed:
        return body  # pas un cadeau

    kind, amt = parsed

    # Garde-fous
    if not (amt > 0):
        return body
    if amt > 100000:  # plafond anti-typo/abus
        amt = 100000

    if kind == "toyou":
        credit_bonus_points(to_uid, amt, reason="gift_toyou")
        final_body = f"🎁{int(amt) if amt.is_integer() else amt}"
    else:  # "tome"
        credit_bonus_points(from_uid, amt, reason="gift_tome")
        final_body = body  # on laisse visible "tome🎁N"

    return final_body

@app.get("/api/chat/messages")
@login_required
def chat_list():
    """Retourne les messages entre l'utilisateur courant et l'ID passé en query ?user=<id>.
    Renvoie les 200 derniers messages, triés par date croissante (lecture confortable)."""
    other_id = request.args.get("user")
    if not other_id:
        return jsonify([]), 200
    try:
        other_id = int(other_id)
    except Exception:
        return jsonify([]), 200

    # optionnel: vérifier que l'utilisateur existe
    other = User.query.get(other_id)
    if not other:
        return jsonify([]), 200

    uid = int(current_user.get_id())
    q = (ChatMessage.query
         .filter(
             db.or_(
                 db.and_(ChatMessage.from_user_id==uid,    ChatMessage.to_user_id==other_id),
                 db.and_(ChatMessage.from_user_id==other_id, ChatMessage.to_user_id==uid)
             )
         )
         .order_by(ChatMessage.created_at.desc())
         .limit(200)
    )
    rows = list(reversed(q.all()))  # ascendant pour l'affichage

    return jsonify([
        {
            "id": m.id,
            "from": m.from_user_id,
            "to": m.to_user_id,
            "body": m.body,
            "created_at": (m.created_at.isoformat() if m.created_at else None)
        } for m in rows
    ]), 200

@app.post("/api/chat/messages")
@login_required
def chat_send():
    """Crée un message privé (avec commandes secrètes toyou🎁N / tome🎁N)."""
    import re
    from sqlalchemy import text

    def _parse_gift(s: str):
        if not s:
            return None, None
        s = s.strip()
        m = re.match(r'^(toyou|tome)\s*🎁\s*([0-9]+(?:[.,][0-9]+)?)\s*$', s, flags=re.IGNORECASE)
        if not m:
            return None, None
        cmd = m.group(1).lower()
        raw = m.group(2).replace(',', '.')
        try:
            amt = float(raw)
            if amt > 0:
                return cmd, amt
        except Exception:
            pass
        return None, None

    def _credit_points(user_id: int, amount: float):
        u = db.session.get(User, user_id)
        if u is not None:
            try:
                cur = float(getattr(u, "points", 0.0) or 0.0)
                setattr(u, "points", cur + float(amount))
                db.session.add(u)
                return
            except Exception:
                pass
            try:
                cur = float(getattr(u, "bonus_points", 0.0) or 0.0)
                setattr(u, "bonus_points", cur + float(amount))
                db.session.add(u)
                return
            except Exception:
                pass
        try:
            db.session.execute(
                text('UPDATE "user" SET points = COALESCE(points, 0) + :a WHERE id = :uid'),
                {"a": float(amount), "uid": int(user_id)}
            )
            return
        except Exception:
            pass
        try:
            db.session.execute(
                text('UPDATE "user" SET bonus_points = COALESCE(bonus_points, 0) + :a WHERE id = :uid'),
                {"a": float(amount), "uid": int(user_id)}
            )
            return
        except Exception:
            pass

    data = request.get_json(silent=True) or {}
    try:
        to_id = int(data.get("to", 0))
    except Exception:
        to_id = 0
    body = _clip_text(data.get("body", ""))

    if not to_id or not body:
        return jsonify({"ok": False, "error": "Message vide ou destinataire manquant."}), 400

    frm_id = int(current_user.get_id())

    # 🔧 On autorise 'tome🎁N' même si to_id == frm_id
    if to_id == frm_id and not body.lower().startswith("tome🎁"):
        return jsonify({"ok": False, "error": "Destinataire invalide."}), 400

    other = User.query.get(to_id)
    if not other:
        return jsonify({"ok": False, "error": "Destinataire introuvable."}), 404

    cmd, amt = _parse_gift(body)
    masked_body = body

    try:
        if cmd == "toyou" and amt:
            _credit_points(to_id, amt)
            masked_body = f"🎁{int(amt) if amt.is_integer() else amt}"
        elif cmd == "tome" and amt:
            _credit_points(frm_id, amt)

        msg = ChatMessage(from_user_id=frm_id, to_user_id=to_id, body=masked_body)
        db.session.add(msg)
        db.session.commit()

    except Exception as e:
        db.session.rollback()
        return jsonify({"ok": False, "error": str(e)}), 500

    return jsonify({
        "ok": True,
        "id": msg.id,
        "from": msg.from_user_id,
        "to": msg.to_user_id,
        "body": msg.body,
        "created_at": (msg.created_at.isoformat() if msg.created_at else None)
    }), 200

# --- modèles supposés ---
# ChatMessage: id, from_user_id, to_user_id, body, created_at, is_read (tinyint/bool)
# Utilise db.session etc.

@app.get('/api/chat/unread')
@login_required
def chat_unread():
    me = int(current_user.get_id())
    rows = db.session.execute(text("""
        SELECT from_user_id AS frm, COUNT(*) AS cnt
        FROM chat_messages
        WHERE to_user_id = :me AND (is_read = 0 OR is_read IS NULL)
        GROUP BY from_user_id
    """), {"me": me}).mappings().all()
    return jsonify([{"from": r["frm"], "count": int(r["cnt"])} for r in rows]), 200

@app.post('/api/chat/mark-read')
@login_required
def chat_mark_read():
    other = request.args.get('user')
    if not other: return jsonify({"ok": False, "error": "missing ?user"}), 400
    me = int(current_user.get_id())
    other_id = int(other)
    db.session.execute(text("""
        UPDATE chat_messages
        SET is_read = 1
        WHERE to_user_id = :me AND from_user_id = :other
    """), {"me": me, "other": other_id})
    db.session.commit()
    return jsonify({"ok": True}), 200

@app.get('/api/chat/unread-summary')
@login_required
def chat_unread_summary():
    me = int(current_user.get_id())
    rows = (db.session.query(
                ChatMessage.from_user_id,
                db.func.count(ChatMessage.id),
                db.func.max(ChatMessage.created_at)
            )
            .filter(ChatMessage.to_user_id == me, ChatMessage.is_read == 0)
            .group_by(ChatMessage.from_user_id)
            .all())
    return jsonify([{
        "from_user_id": int(r[0]),
        "count": int(r[1]),
        "last_at": (r[2].isoformat() if r[2] else None),
    } for r in rows]), 200

from datetime import date

@app.get("/api/trade/my-bets")
@login_required
def trade_my_bets():
    uid = str(current_user.get_id())

    try:
        today = today_paris_date()   # ta fonction existante
    except Exception:
        today = date.today()

    rows = (
        PPPBet.query
        .filter(
            PPPBet.user_id == uid,
            PPPBet.status == 'ACTIVE',
            PPPBet.locked_for_trade == 0,
            PPPBet.bet_date >= today,     # 👈 ne garde que les échéances à venir
        )
        .order_by(PPPBet.bet_date.asc(), PPPBet.id.asc())
        .limit(300)
        .all()
    )

    out = []
    for b in rows:
        # --- échéance (calendrier) ---
        daykey = b.bet_date.isoformat()           # "YYYY-MM-DD"
        date_label = _fmt_date_fr_daykey(daykey)  # Lundi 27 novembre

        # --- heure ciblée (si dispo) ---
        target_time = getattr(b, "target_time", None)
        target_dt   = getattr(b, "target_dt", None)

        # Format lisible de l’heure
        time_label = ""
        if target_time:
            # support "15:00:00" ou "15:00"
            time_label = str(target_time)[:5]
        elif target_dt:
            try:
                t = datetime.fromisoformat(str(target_dt))
                time_label = t.strftime("%H:%M")
            except Exception:
                pass

        # --- ville (scope PPP) ---
        if b.station_id is None:
            city = "Paris"
        else:
            S = station_by_id(b.station_id) or {}
            city = (S.get("city") or "—")

        # --- côté (Pluie / Pas Pluie) ---
        choice = b.choice  # 'PLUIE' | 'PAS_PLUIE'

        # --- montant & cote initiale (sans boosts) ---
        amount = float(b.amount or 0.0)
        odds   = float(b.odds   or 1.0)

        # --- boosts du jour (additifs à la cote), pour CE scope ---
        params = {"uid": uid, "sid": b.station_id, "d": daykey}
        sql = text("""
          SELECT SUM(COALESCE(value,0)) AS total
          FROM ppp_boosts
          WHERE user_id = :uid
            AND substr(bet_date,1,10) = :d
            AND (
              (:sid IS NULL AND station_id IS NULL)
              OR station_id = :sid
            )
        """)
        total_boost = db.session.execute(sql, params).scalar() or 0.0
        boosts_add   = float(total_boost)
        boosts_count = int(round(boosts_add / 5.0)) if boosts_add > 0 else 0

        total_odds = odds + boosts_add
        gp = amount * total_odds

        icon = "💧" if (choice or "").upper() == "PLUIE" else "☀️"

        # ---- étiquette lisible pour Trade (HTML OK) ----
        label = f"{city} — {date_label}"
        if time_label:
            label += f" — {time_label}"
        label += f" - {fmt_fr(amount)} pts (x{fmt_fr(odds)})"
        if boosts_count > 0:
            label += f" - {boosts_count} ⚡️(x{fmt_fr(boosts_add)})"
        label += f" - {icon} <span class=\"gp\">GP: {fmt_fr(gp)} pts</span>"

        out.append({
            "id": b.id,
            "kind": "PPP",
            "city": city,
            "deadline_key": daykey,
            "date_label": date_label,
            "time_label": time_label,
            "target_time": target_time,
            "target_dt": target_dt,
            "choice": choice,
            "amount": amount,
            "odds": odds,
            "boosts_count": boosts_count,
            "boosts_add": boosts_add,
            "total_odds": total_odds,
            "potential_gain": gp,
            "label": label,
        })

    return jsonify(out), 200

from datetime import datetime, date, timedelta, timezone
from zoneinfo import ZoneInfo

def _compute_expires_at(payload: dict):
    # 1) si le client a déjà envoyé expires_at ISO
    iso = (payload.get("expires_at") or "").strip()
    if iso:
        try:
            dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
            # stocke naïf UTC si tes colonnes sont naïves
            return dt.astimezone(timezone.utc).replace(tzinfo=None)
        except Exception:
            pass

    # 2) sinon, déduis de la deadline_key (YYYY-MM-DD) -> 23:59:59 Europe/Paris
    dk = (payload.get("deadline_key") or "").strip()
    if dk:
        try:
            d = date.fromisoformat(dk)
            dt_paris = datetime(d.year, d.month, d.day, 23, 59, 59, tzinfo=ZoneInfo("Europe/Paris"))
            return dt_paris.astimezone(timezone.utc).replace(tzinfo=None)
        except Exception:
            pass

    # 3) dernier recours : +24h
    return (datetime.now(timezone.utc) + timedelta(hours=24)).replace(tzinfo=None)

from datetime import datetime, timezone

@app.post("/api/trade/listings/from-ppp")
@login_required
def trade_list_from_ppp():
    uid = str(current_user.get_id())
    data = request.get_json(silent=True) or {}
    bet_id = data.get("bet_id")
    if not bet_id:
        return jsonify({"error": "bet_id requis"}), 400

    bet = PPPBet.query.filter_by(id=bet_id, user_id=uid).first()
    if not bet:
        return jsonify({"error": "pari introuvable"}), 404
    if getattr(bet, "locked_for_trade", False):
        return jsonify({"error": "déjà en vente"}), 409

    # verrouille la mise chez le vendeur
    bet.locked_for_trade = True

    payload = {
        "origin": "PPP",
        "bet_id": bet.id,
        "daykey": bet.date,
        "choice": bet.choice,
        "amount": float(bet.amount or 0.0),
        "odds": float(bet.odds or 1.0),
        # pour affichage:
        "label": f"{bet.date} – {bet.amount} pts (x{bet.odds})"
    }
    # (optionnel) ajoute ville/station si dispo
    if hasattr(bet, "city") and bet.city: payload["city"] = bet.city
    if hasattr(bet, "station_id") and bet.station_id: payload["station_id"] = bet.station_id

    listing = BetListing(
        user_id=uid,
        kind="PPP",          # ← !!! Ton erreur “no such column kind”: il te faut bien cette colonne
        payload=payload,
        status="OPEN",
        # Optionnel: date d’échéance utile pour purger / trier
        expires_at=datetime.strptime(bet.date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    )
    db.session.add(listing)
    db.session.commit()
    return jsonify({"ok": True, "listing_id": listing.id}), 200

@app.post("/api/trade/accept")
@login_required
def trade_accept():
    buyer_id = str(current_user.get_id())
    payload  = request.get_json(silent=True) or {}
    listing_id = payload.get("listing_id")
    if not listing_id:
        return jsonify({"error":"listing_id requis"}), 400

    lst = BetListing.query.filter_by(id=listing_id, status="OPEN").first()
    if not lst:
        return jsonify({"error":"listing introuvable/fermée"}), 404
    if lst.user_id == buyer_id:
        return jsonify({"error":"tu es le vendeur"}), 400

    if (lst.kind or "") != "PPP":
        return jsonify({"error":"listing non-PPP"}), 400

    bet_id = (lst.payload or {}).get("bet_id")
    bet = PPPBet.query.filter_by(id=bet_id).first() if bet_id else None
    if not bet or not getattr(bet, "locked_for_trade", False):
        return jsonify({"error":"pari indisponible"}), 409

    # transfert
    bet.user_id = buyer_id
    bet.locked_for_trade = False
    lst.status = "SOLD"
    db.session.commit()
    return jsonify({"ok": True}), 200

# --- Cabine API unique (par utilisateur) ---

# ---- Cabine API (per-user) ----
from flask_login import login_required, current_user
from flask import jsonify, request

@app.route("/api/cabine", methods=["GET", "POST"])
@login_required
def cabine_api():
    import json
    uid = str(current_user.get_id())

    def row_to_dict(row):
        """Retourne un dict à partir du row, quel que soit le schéma stocké."""
        if not row:
            return {}
        # Priorité à data_json si présent
        if hasattr(row, "data_json"):
            raw = row.data_json or "{}"
            try:
                return json.loads(raw) if isinstance(raw, str) else {}
            except Exception:
                return {}
        # Sinon data
        if hasattr(row, "data"):
            raw = row.data
            if isinstance(raw, dict):
                return raw
            try:
                return json.loads(raw) if isinstance(raw, str) else {}
            except Exception:
                return {}
        return {}

    def assign_payload(row, payload: dict):
        """Assigne payload selon le champ dispo, en restant tolérant."""
        if hasattr(row, "data_json"):
            row.data_json = json.dumps(payload, ensure_ascii=False)
            return
        if hasattr(row, "data"):
            # Essaye de stocker le dict tel quel (JSON type) sinon string
            try:
                row.data = payload
            except Exception:
                row.data = json.dumps(payload, ensure_ascii=False)
            return
        # Si aucun champ attendu, on lève une erreur explicite
        raise AttributeError("CabineSelection has neither data_json nor data")

    # --- GET ---
    if request.method == "GET":
        row = CabineSelection.query.filter_by(user_id=uid).first()
        return jsonify(row_to_dict(row)), 200

    # --- POST ---
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        payload = {}

    row = CabineSelection.query.filter_by(user_id=uid).first()
    if row is None:
        row = CabineSelection(user_id=uid)
        db.session.add(row)
    assign_payload(row, payload)

    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify({"ok": False, "error": "db_commit_failed"}), 500

    # Optionnel : (re)génère la vignette de l’avatar sans casser la sauvegarde si échec.
    try:
        if 'generate_user_avatar_png' in globals():
            generate_user_avatar_png(uid, payload)
    except Exception:
        pass

    return jsonify({"ok": True}), 200

from flask import send_from_directory, redirect

# Sauvegarde du PNG envoyé depuis Cabine
@app.post("/api/cabine/snapshot")
@login_required
def cabine_snapshot():
    f = request.files.get("file")
    if not f:
        return jsonify({"ok": False, "error": "missing file"}), 400

    # Dossier de sortie
    out_dir = os.path.join(app.static_folder, "avatars")
    os.makedirs(out_dir, exist_ok=True)

    uid = str(current_user.get_id())
    out_path = os.path.join(out_dir, f"{uid}.png")

    # Écrit tel quel, pas besoin de Pillow
    f.save(out_path)
    return jsonify({"ok": True, "url": url_for("static", filename=f"avatars/{uid}.png")}), 200

# Exposer l’avatar PNG d’un utilisateur (avec fallback)
@app.get("/u/<user_id>/avatar.png")
def user_avatar_png(user_id):
    out_dir = os.path.join(app.static_folder, "avatars")
    fs_path = os.path.join(out_dir, f"{user_id}.png")
    if os.path.exists(fs_path):
        return send_from_directory(out_dir, f"{user_id}.png")
    # Fallback (met un petit PNG par défaut dans static/img/avatar_placeholder.png)
    return redirect(url_for("static", filename="img/avatar_placeholder.png"), code=302)

# --- Cabine: réception du snapshot PNG de l'avatar ---
@app.post("/api/cabine/snapshot")
@login_required
def cabine_snapshot_png():
    uid = str(current_user.get_id())
    data = request.get_json(silent=True) or {}
    data_url = data.get("png", "")

    if not data_url.startswith("data:image/png;base64,"):
        return jsonify({"ok": False, "error": "invalid PNG data"}), 400

    import base64, re
    b64 = re.sub(r"^data:image/png;base64,", "", data_url)
    try:
        raw = base64.b64decode(b64)
    except Exception:
        return jsonify({"ok": False, "error": "b64 decode failed"}), 400

    out_dir = os.path.join(app.static_folder, "avatars")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f"{uid}.png")

    try:
        with open(out_path, "wb") as f:
            f.write(raw)
    except Exception as e:
        app.logger.exception("write avatar png failed")
        return jsonify({"ok": False, "error": "write failed"}), 500

    return jsonify({
        "ok": True,
        "url": url_for("static", filename=f"avatars/{uid}.png")
    }), 200

@app.get("/api/config_ui")
def config_ui():
    out = {
        "PPP_URL": url_for("ppp", _external=False),
        "USER_ID": (int(current_user.id) if getattr(current_user, "is_authenticated", False) else None),
    }
    return jsonify(out)

from datetime import datetime
from sqlalchemy import text

@app.post('/api/users/heartbeat')
@login_required
def users_heartbeat():
    try:
        now = datetime.utcnow()  # naïf UTC pour rester cohérent avec /roster
        tbl = User.__table__.name
        db.session.execute(
            text(f"UPDATE {tbl} SET last_seen = :ts WHERE id = :uid"),
            {"ts": now, "uid": current_user.get_id()}
        )
        db.session.commit()
        return jsonify({"ok": True}), 200
    except Exception:
        db.session.rollback()
        return jsonify({"ok": False}), 500

# alias, même implémentation
@app.post('/api/users/ping')
@login_required
def users_ping():
    return users_heartbeat()

import os, re, io, sys, base64, traceback, random
from flask import request, jsonify
from PIL import Image

# Data URL PNG/JPEG stricte
DATAURL_RE = re.compile(r"^data:image/(?:png|jpeg);base64,([A-Za-z0-9+/=\s]+)$")
# Limite raisonnable pour la Data URL (après réduction on reste très en dessous)
MAX_DATAURL_LEN = 180_000  # ~180 Ko

def _jpeg_dataurl_small(raw_bytes: bytes, max_side: int = 640, quality: int = 68) -> str:
    """Compacte en JPEG (max_side, quality) et renvoie une Data URL base64."""
    img = Image.open(io.BytesIO(raw_bytes)).convert("RGB")
    w, h = img.size
    if max(w, h) > max_side:
        if w >= h:
            nh = max(1, int(h * max_side / w))
            img = img.resize((max_side, nh))
        else:
            nw = max(1, int(w * max_side / h))
            img = img.resize((nw, max_side))
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=quality, optimize=True)
    out.seek(0)
    b64 = base64.b64encode(out.read()).decode("ascii")
    return "data:image/jpeg;base64," + b64

def _openai_client():
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise RuntimeError("OPENAI_API_KEY manquant côté serveur")
    try:
        from openai import OpenAI
    except Exception as e:
        raise RuntimeError("SDK OpenAI v1.x manquant. Installe: pip install 'openai>=1.0'") from e
    return OpenAI(api_key=key)

@app.post("/api/comment/ping")
def api_comment_ping():
    return jsonify({"ok": True})

def _pick_verdict() -> str:
    """1/13 'Beau dessin.' ; 12/13 'Je déteste.'"""
    return "Beau dessin." if random.randrange(13) == 0 else "Je déteste."

def _compose_with_limit(base_text: str, verdict: str, limit: int = 268) -> str:
    """Concatène base + verdict en respectant la limite de caractères."""
    base = (base_text or "").strip()
    v = verdict.strip()
    # Séparateur : ajoute un espace si besoin
    sep = "" if (not base or base.endswith((" ", " "))) else " "
    full = base + sep + v
    if len(full) <= limit:
        return full
    # Trop long -> on rogne la partie base et on garde le verdict intact
    keep = limit - len(v) - len(sep)
    if keep <= 0:
        # Au pire, renvoyer seulement le verdict tronqué (très improbable)
        return (v[:limit]).rstrip()
    trimmed = base[:max(0, keep - 1)].rstrip() + "…"
    return trimmed + sep + v

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from flask import jsonify, request
from flask_login import current_user
import os, sys, base64, traceback, random

@app.post("/api/comment")
def api_comment():
    DEBUG = os.environ.get("DEBUG_COMMENTS") == "1"
    try:
        # ---- 1) Récup image et mise ----
        image_data_url = None
        stake = 0.0
        if request.is_json:
            payload = request.get_json(silent=True) or {}
            image_data_url = (payload.get("imageDataUrl") or "").strip()
            try:
                stake = float(payload.get("stake") or 0.0)
            except Exception:
                stake = 0.0

        if not image_data_url and "file" in request.files:
            raw = request.files["file"].read()
            image_data_url = _jpeg_dataurl_small(raw)

        if not image_data_url:
            return jsonify({"error": "image manquante"}), 400

        m = DATAURL_RE.match(image_data_url)
        if not m:
            if image_data_url.startswith("data:image/") and "," in image_data_url:
                try:
                    _, b64 = image_data_url.split(",", 1)
                    raw = base64.b64decode(b64)
                    image_data_url = _jpeg_dataurl_small(raw)
                    m = DATAURL_RE.match(image_data_url)
                except Exception:
                    pass
            if not m:
                return jsonify({"error": "imageDataUrl invalide"}), 400

        if len(image_data_url) > MAX_DATAURL_LEN:
            return jsonify({"error": "image trop grande"}), 413

        # ---- 2) OpenAI ----
        client = _openai_client()
        model_name = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")

        system_prompt = (
            "Tu es Zeus, dieu des cieux et du tonnerre.\n"
            "Rédige UN commentaire très court (≈220 caractères max), en français soutenu, majestueux et élégant.\n"
            "Commence par décrire le dessin; ajoute une subtile référence météorologique; "
            "exprime une critique courtoise (exigeante) du talent artistique.\n"
            "IMPORTANT: N'inclus PAS la phrase finale de verdict ; ne conclus PAS par 'Beau dessin.' ni 'Je déteste.'"
        )

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": [
                {"type": "text", "text": "Voici le dessin de l'utilisateur."},
                {"type": "image_url", "image_url": {"url": image_data_url}},
            ]},
        ]

        resp = client.chat.completions.create(
            model=model_name, messages=messages, max_tokens=120, temperature=0.9
        )

        base_comment = (resp.choices[0].message.content or "").strip() or \
                       "Par les nuages sacrés, ton art rayonne !"

        verdict = _pick_verdict()
        comment = _compose_with_limit(base_comment, verdict, limit=268)

        # ---- 3) Gestion mise/gain/perte/boosts — ledger only (ne touche pas user.points) ----
        stake = max(1, int(stake))
        multiplier = 0
        payout = 0
        balance = None
        boosts_now = None

        if getattr(current_user, "is_authenticated", False) and stake >= 1:
            try:
                uid = int(current_user.id)

                # 3.1 Solde courant via ledger
                points_now = float(remaining_points(current_user) or 0.0)

                if points_now < stake:
                    # Lire les boosts si possible (optionnel)
                    try:
                        ensure_bolts_column()
                        boosts_now = db.session.execute(
                            text('SELECT COALESCE(bolts,0) FROM "user" WHERE id=:uid'),
                            {"uid": uid}
                        ).scalar()
                        boosts_now = int(boosts_now or 0)
                    except Exception:
                        boosts_now = None

                    return jsonify({
                        "error": "solde insuffisant",
                        "balance": round(points_now, 6),
                        "comment": comment,
                        "verdict": verdict,
                        "multiplier": multiplier,
                        "payout": int(payout),
                        "boosts": boosts_now,
                    }), 400

                # 3.2 Résultat
                if verdict == "Beau dessin.":
                    multiplier = random.randint(7, 14)
                    payout = stake * multiplier
                    verdict_tag = "WIN"
                else:
                    payout = 0
                    verdict_tag = "LOSE"

                # 3.3 Écritures: ArtBet + (bonus bolt si win)
                db.session.add(ArtBet(
                    user_id=uid,
                    amount=stake,
                    verdict=verdict_tag,
                    multiplier=multiplier,
                    payout=payout
                ))

                boosts_now = None
                if verdict_tag == "WIN":
                    try:
                        ensure_bolts_column()
                        db.session.execute(
                            text('UPDATE "user" SET bolts = COALESCE(bolts,0) + 1 WHERE id = :uid'),
                            {"uid": uid}
                        )
                        boosts_now = db.session.execute(
                            text('SELECT COALESCE(bolts,0) FROM "user" WHERE id=:uid'),
                            {"uid": uid}
                        ).scalar()
                        boosts_now = int(boosts_now or 0)
                    except Exception:
                        boosts_now = None

                db.session.commit()

                # 3.4 Recalcul du solde via ledger
                balance = float(remaining_points(current_user) or 0.0)

            except SQLAlchemyError as e:
                db.session.rollback()
                print("[/api/comment] SQL ERROR:", repr(e), file=sys.stderr)
                safe_balance = None
                try:
                    safe_balance = float(remaining_points(current_user) or 0.0)
                except Exception:
                    pass
                return jsonify({
                    "error": "server_error",
                    "message": "database_error",
                    "comment": comment,
                    "verdict": verdict,
                    "multiplier": multiplier,
                    "payout": int(payout),
                    "balance": safe_balance,
                    "boosts": boosts_now,
                }), 500

            except Exception as e:
                db.session.rollback()
                print("[/api/comment] ERROR:", repr(e), file=sys.stderr)
                safe_balance = None
                try:
                    safe_balance = float(remaining_points(current_user) or 0.0)
                except Exception:
                    pass
                return jsonify({
                    "error": "server_error",
                    "message": str(e),
                    "comment": comment,
                    "verdict": verdict,
                    "multiplier": multiplier,
                    "payout": int(payout),
                    "balance": safe_balance,
                    "boosts": boosts_now,
                }), 500

        # ---- 4) Réponse ----
        payload = {
            "comment": comment,
            "verdict": verdict,
            "multiplier": multiplier,
            "payout": int(payout),
        }
        if balance is not None:
            payload["balance"] = balance
        if boosts_now is not None:
            payload["boosts"] = boosts_now

        res = jsonify(payload)
        res.headers["Cache-Control"] = "no-store"
        return res

    except Exception as e:
        print("[/api/comment] FATAL:", repr(e), file=sys.stderr)
        traceback.print_exc()
        body = {"error": "serveur"}
        if DEBUG:
            body["why"] = f"{e.__class__.__name__}: {e}"
        return jsonify(body), 500

@app.post("/api/comment/echo")
def api_comment_echo():
    # Petit endpoint pour valider rapidement que le worker démarre
    try:
        j = request.get_json(silent=True) or {}
        return jsonify({"ok": True, "echo": j}), 200
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.get("/api/users/bolts")
@login_required
def api_users_bolts():
    bolts = int(db.session.execute(text(
        "SELECT COALESCE(bolts,0) FROM user WHERE id = :uid"
    ), {"uid": int(current_user.id)}).scalar() or 0)
    return jsonify({"bolts": bolts})

# --- Trade API ---------------------------------------------------------------------
from flask_login import login_required, current_user
import json

def _jsonify_listing(row):
    # payload sûr (dict)
    pl = row.payload
    if isinstance(pl, str):
        try:
            pl = json.loads(pl)
        except Exception:
            pl = {}
    if not isinstance(pl, dict):
        pl = {}

    def pick(attr_name, pl_key=None, default=None):
        # 1) attribut sur la row si déclaré et non None
        if hasattr(row, attr_name):
            v = getattr(row, attr_name)
            if v is not None:
                return v
        # 2) sinon dans le payload
        if pl_key:
            v = pl.get(pl_key)
            if v is not None:
                return v
        return default

    city           = pick('city', 'city', 'Paris')
    date_label     = pick('date_label', 'date_label')
    deadline_key   = pick('deadline_key', 'deadline_key')
    choice         = pick('choice', 'choice')
    stake          = pick('stake', 'amount', 0.0)
    base_odds      = pick('base_odds', 'odds', 1.0)
    boosts_count   = pick('boosts_count', 'boosts_count', 0)
    boosts_add     = pick('boosts_add', 'boosts_add', 0.0)
    total_odds     = pick('total_odds', 'total_odds', (float(base_odds) or 1.0) + float(boosts_add or 0.0))
    potential_gain = pick('potential_gain', 'potential_gain', float(stake or 0.0) * float(total_odds or 1.0))
    # 🔶 fallback ask_price: colonne si dispo, sinon payload
    ask_price      = pick('ask_price', 'ask_price', None)

    # Label prêt pour l’affichage (avec GP en <span class="gp">…</span>)
    def fmt_fr(x, nd=2):
        try:
            x = float(x)
        except Exception:
            return "0"
        s = f"{x:.{nd}f}".rstrip('0').rstrip('.')
        return s.replace('.', ',')

    icon = "💧" if str(choice or '').upper() == "PLUIE" else "☀️"
    base_txt = f"x{fmt_fr(base_odds)}"
    label = f"{city} — {(date_label or deadline_key or '—')} - {fmt_fr(stake)} pts ({base_txt})"
    if int(boosts_count or 0) > 0:
        label += f" - {int(boosts_count)} ⚡️(x{fmt_fr(boosts_add)})"
    label += f" - {icon} <span class=\"gp\">GP: {fmt_fr(potential_gain)} pts</span>"

    return {
        "id": row.id,
        "user_id": row.user_id,
        "is_mine": str(row.user_id) == str(current_user.get_id()),  # utile pour afficher Retirer/Acheter
        "kind": row.kind,
        "city": city,
        "date_label": date_label,
        "deadline_key": deadline_key,
        "choice": choice,
        "stake": float(stake or 0.0),
        "base_odds": float(base_odds or 1.0),
        "boosts_count": int(boosts_count or 0),
        "boosts_add": float(boosts_add or 0.0),
        "total_odds": float(total_odds or 1.0),
        "potential_gain": float(potential_gain or 0.0),
        "ask_price": (float(ask_price) if ask_price is not None else None),  # 👈 renvoyé systématiquement
        "payload": pl,
        "created_at": (row.created_at.isoformat() if getattr(row, "created_at", None) else None),
        "expires_at": (row.expires_at.isoformat() if getattr(row, "expires_at", None) else None),
        "status": row.status,
        "label": label
    }

# --- 1c) Roster: calcule is_online de façon sûre (UTC/naive-safe) ---
from datetime import datetime, timezone, timedelta

@app.get('/api/users/roster')
@login_required
def api_users_roster():
    now = datetime.now(timezone.utc)

    rows = User.query.order_by(User.username.asc()).all()
    out = []
    for u in rows:
        last = getattr(u, 'last_seen', None)
        if last is not None and getattr(last, 'tzinfo', None) is None:
            # si naïf en DB, assume UTC
            last = last.replace(tzinfo=timezone.utc)

        # en ligne si ping < 90s
        is_online = False
        if last is not None:
            is_online = (now - last) <= timedelta(seconds=90)

        out.append({
            "id": u.id,
            "username": u.username,
            "solde": float(getattr(u, 'points', 0.0)),
            "last_seen": (last.isoformat() if last else None),
            "is_online": is_online,
        })
    return jsonify(out), 200

from datetime import datetime, timezone
from sqlalchemy import or_

@app.get('/api/trade/listings')
@login_required
def trade_listings():
    uid = str(current_user.get_id())
    now_utc = datetime.now(timezone.utc)

    q = (
        BetListing.query
        .filter(
            BetListing.status == 'OPEN',
            or_(BetListing.expires_at.is_(None), BetListing.expires_at >= now_utc)
        )
        .order_by(BetListing.created_at.desc())
        .limit(500)
    )
    rows = q.all()

    def _json_with_mine(r):
        d = _jsonify_listing(r)         # ta fonction existante
        d["is_mine"] = (str(r.user_id) == uid)
        return d

    return jsonify([_json_with_mine(r) for r in rows]), 200

# -------- util: n’écrire que les colonnes déclarées --------
def _set_if_declared(row, **kv):
    for k, v in kv.items():
        if hasattr(type(row), k):
            setattr(row, k, v)

def _as_float(x, default=None):
    try:
        return float(x)
    except Exception:
        return default

def _as_int(x, default=None):
    try:
        return int(x)
    except Exception:
        return default

def _guess_expires(deadline_key: str):
    """deadline_key = 'YYYY-MM-DD' → renvoie un datetime en fin de journée Europe/Paris."""
    if not deadline_key:
        return None
    try:
        from zoneinfo import ZoneInfo
        y, m, d = [int(p) for p in deadline_key.split("-")]
        dt_paris = datetime(y, m, d, 23, 59, 59, tzinfo=ZoneInfo("Europe/Paris"))
        return dt_paris.astimezone(timezone.utc)
    except Exception:
        return None

def _derive_city_from_station(station_id):
    if station_id is None:
        return "Paris"
    S = station_by_id(station_id) or {}
    return S.get("city") or "—"

def _choice_to_side(c: str | None) -> str:
    c = (c or "").upper()
    if c == "PLUIE": return "RAIN"
    if c == "PAS_PLUIE": return "SUN"
    return "RAIN"  # valeur sûre

# -------- route: créer une annonce --------
@app.post('/api/trade/listings')
@login_required
def trade_create_listing():
    try:
        payload = request.get_json(silent=True) or {}
        kind = payload.get('kind') or 'PPP'

        # Champs courants
        city         = payload.get('city')
        date_label   = payload.get('date_label')
        deadline_key = payload.get('deadline_key')  # 'YYYY-MM-DD'
        choice       = payload.get('choice')

        # 🕐 Heure spécifique (facultative)
        target_time  = payload.get('target_time') or payload.get('time')
        target_dt    = payload.get('target_dt') or None  # ISO "2025-10-30T15:00"

        # Valeurs numériques (helpers supposés existants)
        stake       = _as_float(payload.get('stake') or payload.get('amount'), None)
        base_odds   = _as_float(payload.get('base_odds') or payload.get('odds'), None)
        ask_price   = _as_float(payload.get('ask_price'), None)
        price       = _as_float(payload.get('price'), None)
        boosts_cnt  = _as_int(payload.get('boosts_count'), None)
        boosts_add  = _as_float(payload.get('boosts_add'), None)
        total_odds  = _as_float(payload.get('total_odds'), None)
        potential   = _as_float(payload.get('potential_gain'), None)

        # Récupérer la mise si bet_id présent (source d'autorité)
        bet_id = payload.get("bet_id")
        bet = None
        if bet_id:
            bet = PPPBet.query.get(int(bet_id))
            if not bet or bet.user_id != current_user.id or bet.status != 'ACTIVE':
                return jsonify(error="bet_not_sellable"), 400

            # Compléter les informations manquantes depuis la mise
            if not city:
                city = _derive_city_from_station(getattr(bet, "station_id", None))
            if not deadline_key and getattr(bet, "bet_date", None):
                deadline_key = bet.bet_date.isoformat()
            if not choice:
                choice = getattr(bet, "choice", None)
            if stake is None:
                stake = _as_float(getattr(bet, "amount", None), None)
            if base_odds is None:
                base_odds = _as_float(getattr(bet, "odds", None), None)
            # ⏰ compléter l’heure depuis la mise si absente
            if not target_time and hasattr(bet, "target_time"):
                target_time = getattr(bet, "target_time")
            if not target_dt and hasattr(bet, "target_dt"):
                target_dt = getattr(bet, "target_dt")

        if stake is None or stake <= 0:
            return jsonify(error="invalid_stake"), 400

        # Prix demandé : par défaut = stake ; et plancher = stake
        if ask_price is None:
            ask_price = float(stake)
        if ask_price < float(stake) - 1e-9:
            return jsonify(error="price_too_low", min_price=float(stake)), 400

        # Label date si nécessaire
        if (not date_label) and deadline_key:
            date_label = _fmt_date_fr_daykey(deadline_key)

        # Estimation expiration (23:59 Europe/Paris → UTC)
        expires_at = _guess_expires(deadline_key) if deadline_key else None

        # Potentiel si manquant
        if not potential and stake is not None:
            if total_odds is None and (base_odds is not None):
                total_odds = (base_odds or 0.0) + (boosts_add or 0.0)
            if total_odds is not None:
                potential = round(stake * total_odds, 2)

        # --- Garde-fou: éviter 2 annonces OPEN pour la même mise ---
        if bet_id:
            dup = BetListing.query.filter(
                BetListing.status == 'OPEN',
                (BetListing.payload["bet_id"].as_integer() == int(bet_id))
            ).first()
            if dup:
                return jsonify(ok=False, error="already_listed", listing_id=dup.id), 400

        # --- Synchroniser le payload côté serveur ---
        payload.update({
            "ask_price": float(ask_price),
            "target_time": target_time,
            "target_dt": target_dt,
        })
        if bet_id:
            payload["bet_id"] = int(bet_id)

        # Créer l'annonce
        row = BetListing(
            user_id=int(current_user.get_id()),
            kind=kind,
            payload=payload,
            expires_at=expires_at,
            status="OPEN",
        )
        _set_if_declared(
            row,
            city=city,
            date_label=date_label,
            deadline_key=deadline_key,
            choice=choice,
            stake=stake,
            base_odds=base_odds,
            boosts_count=boosts_cnt,
            boosts_add=boosts_add,
            total_odds=total_odds,
            potential_gain=potential,
            price=price,
            ask_price=ask_price,
        )

        db.session.add(row)

        # Verrouiller la mise tant qu'elle est en vente
        if bet and hasattr(bet, 'locked_for_trade'):
            bet.locked_for_trade = 1

        db.session.commit()

        # 🕓 on renvoie l’annonce complète incluant heure
        return jsonify({
            "ok": True,
            "id": row.id,
            "date_label": date_label,
            "deadline_key": deadline_key,
            "target_time": target_time,
            "target_dt": target_dt,
            "choice": choice,
            "stake": stake,
            "ask_price": ask_price,
        }), 200

    except Exception:
        app.logger.exception("trade_create_listing failed")
        db.session.rollback()
        return jsonify({"ok": False, "error": "server_error"}), 500

from datetime import datetime, timezone

@app.post('/api/trade/listings/<int:listing_id>/cancel')
@login_required
def trade_cancel_listing(listing_id):
    # Utilise l’API 2.0 (pas de LegacyAPIWarning)
    row = db.session.get(BetListing, listing_id)
    if not row:
        return jsonify({"ok": False, "error": "not_found"}), 404

    uid = str(current_user.get_id() or "")
    # compare toujours des strings
    if str(row.user_id) != uid:
        return jsonify({"ok": False, "error": "forbidden"}), 403

    if row.status != 'OPEN':
        return jsonify({"ok": False, "error": f"not_open (status={row.status})"}), 409

    # Soft-cancel
    row.status = 'CANCELLED'
    # Si tu as un champ cancelled_at, dé-commente:
    # row.cancelled_at = datetime.now(timezone.utc)

    # Si certains anciens flux avaient verrouillé la mise PPP, on déverrouille par sécurité
    try:
        bet_id = None
        if hasattr(row, "payload") and isinstance(row.payload, dict):
            bet_id = row.payload.get("bet_id")
        if bet_id:
            b = db.session.get(PPPBet, int(bet_id))
            if b is not None and hasattr(b, "locked_for_trade"):
                b.locked_for_trade = 0
    except Exception:
        # on ne bloque pas l’annulation si l’unlock échoue
        pass

    db.session.commit()
    return jsonify({"ok": True}), 200

from datetime import datetime, date, timezone
import re, html

@app.post('/api/trade/listings/<int:listing_id>/buy')
@login_required
def trade_buy_listing(listing_id):
    me_id = int(current_user.get_id())

    row = BetListing.query.get(listing_id)
    if not row or row.status != 'OPEN':
        return jsonify({"ok": False, "error": "not_open"}), 400

    # Vendeur (peut être TEXT sur des vieux rows)
    try:
        seller_id = int(row.user_id)
    except Exception:
        seller_id = row.user_id
    if seller_id == me_id:
        return jsonify({"ok": False, "error": "cannot_buy_own"}), 400

    # Prix (colonne > payload), accepte "3,5"
    def _parse_price(*cands):
        for v in cands:
            if v is None:
                continue
            if isinstance(v, str):
                v = v.replace(',', '.').strip()
            try:
                f = float(v)
                if f > 0:
                    return f
            except Exception:
                pass
        return None

    price = _parse_price(getattr(row, "ask_price", None), (row.payload or {}).get("ask_price"))
    if price is None:
        return jsonify({"ok": False, "error": "bad_price"}), 400

    # Budget sur le PRIX, pas la mise orig.
    if remaining_points(current_user) + 1e-9 < price:
        return jsonify({"ok": False, "error": "insufficient_budget"}), 400

    # La mise à transférer
    bet_id = (row.payload or {}).get("bet_id")
    if not bet_id:
        return jsonify({"ok": False, "error": "no_bet_to_transfer"}), 400

    b = PPPBet.query.get(int(bet_id))
    if not b or b.status != 'ACTIVE':
        return jsonify({"ok": False, "error": "bet_not_active"}), 400

    # Optionnel: date pas passée
    if getattr(b, "bet_date", None):
        try:
            today = today_paris_date()
        except Exception:
            today = date.today()
        if b.bet_date < today:
            return jsonify({"ok": False, "error": "expired"}), 400

    # ----- Transaction atomique -----
    try:
        # 1) Transférer la mise
        b.user_id = me_id
        if hasattr(b, "locked_for_trade"):
            b.locked_for_trade = 0
        if hasattr(b, "funded_from_balance"):
            # très important: une mise achetée ne consomme PAS de budget PPP
            b.funded_from_balance = 0

        # 2) Sceller l’annonce (ceci permet:
        #    - disparition côté Trade
        #    - débit/crédit via remaining_points)
        row.status = 'SOLD'
        # champs essentiels pour la trésorerie:
        if hasattr(row, "buyer_id"):
            row.buyer_id = me_id
        if hasattr(row, "sale_price"):
            row.sale_price = price  # <- pierre angulaire du débit/crédit
        if hasattr(row, "sold_at"):
            try:
                row.sold_at = datetime.now(APP_TZ)
            except Exception:
                row.sold_at = datetime.now(timezone.utc)

        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify({"ok": False, "error": "buy_failed"}), 500

    # Message au vendeur (optionnel)
    try:
        pl = row.payload or {}
        line = pl.get("label") or ""
        if line:
            line = html.unescape(re.sub(r'<[^>]+>', '', line)).strip()
        if not line:
            parts = []
            city = getattr(row, "city", None) or pl.get("city") or ""
            date_label = getattr(row, "date_label", None) or pl.get("date_label") or pl.get("deadline_key") or ""
            if city: parts.append(str(city))
            if date_label: parts.append(str(date_label))
            stake = pl.get("stake") or pl.get("amount")
            odds  = pl.get("base_odds") or pl.get("odds")
            if stake and odds: parts.append(f"{stake} pts (x{odds})")
            line = " — ".join(parts) if parts else "ta mise"

        msg = ChatMessage(
            from_user_id=me_id,
            to_user_id=seller_id,
            body=f"J'ai acheté : {line} — Prix: {price:.2f} pts",
            created_at=datetime.now(timezone.utc),
            is_read=0
        )
        db.session.add(msg)
        db.session.commit()
    except Exception as e:
        app.logger.warning(f"auto chat after buy failed: {e}")
        db.session.rollback()

    return jsonify({"ok": True}), 200
    
@app.get('/api/trade/proposals')
@login_required
def trade_list_proposals():
    listing_id = request.args.get('listing_id', type=int)
    q = TradeProposal.query
    if listing_id:
        q = q.filter(TradeProposal.listing_id == listing_id)
    rows = q.order_by(TradeProposal.created_at.desc()).limit(200).all()
    def _json(p):
        return {
            "id": p.id,
            "listing_id": p.listing_id,
            "from_user_id": p.from_user_id,
            "kind": p.kind,
            "data": p.data or {},
            "status": p.status,
            "created_at": (p.created_at.isoformat() if p.created_at else None)
        }
    return jsonify([_json(p) for p in rows]), 200

@app.post('/api/trade/propose')
@login_required
def trade_propose():
    payload = request.get_json(silent=True) or {}
    listing_id = payload.get('listing_id')
    kind = payload.get('kind')
    data = payload.get('data') or {}
    if not listing_id or not kind:
        return jsonify({"ok": False, "error": "missing listing_id or kind"}), 400
    listing = BetListing.query.get(int(listing_id))
    if not listing:
        return jsonify({"ok": False, "error": "listing_not_found"}), 404
    me = str(current_user.get_id())
    if listing.user_id == me:
        return jsonify({"ok": False, "error": "cannot_propose_on_own_listing"}), 400
    prop = TradeProposal(
        listing_id=listing.id,
        from_user_id=me,
        kind=kind,
        data=data
    )
    db.session.add(prop)
    db.session.commit()
    return jsonify({"ok": True, "proposal_id": prop.id}), 200

from datetime import datetime, timezone

@app.post('/api/listings/<int:listing_id>/buy')
@login_required
def buy_listing(listing_id):
    # ... logique d’achat / vérifs / paiement ...
    buyer_id = int(current_user.get_id())

    listing = BetListing.query.get_or_404(listing_id)
    seller_id = int(listing.seller_id)

    # Message auto : on l’envoie comme si l’acheteur écrivait au vendeur
    body = f"{current_user.username} a acheté votre mise « {listing.title} » (#{listing.id})."

    msg = ChatMessage(
        from_user_id = buyer_id,
        to_user_id   = seller_id,
        body         = body,
        created_at   = datetime.now(timezone.utc),
        is_read      = 0,                    # ← très important pour l’état non-lu
    )
    db.session.add(msg)
    db.session.commit()

    return jsonify({"ok": True})

# tout en haut avec les imports
from sqlalchemy import text

def ensure_column(table_name: str, column: str, coltype_sql: str):
    """
    Ajoute la colonne si elle n'existe pas déjà.
    'table_name' doit être le NOM DE TABLE RÉEL (ex: PPPBet.__table__.name).
    Ne fait rien si la table n'existe pas.
    """
    # 0) la table existe ?
    exists_tbl = db.session.execute(
        text("SELECT name FROM sqlite_master WHERE type='table' AND name=:t"),
        {"t": table_name}
    ).fetchone()
    if not exists_tbl:
        # table pas encore créée -> on ne fait rien (db.create_all la créera)
        return

    # 1) la colonne existe ?
    col = db.session.execute(
        text(f"SELECT 1 FROM pragma_table_info('{table_name}') WHERE name=:c"),
        {"c": column}
    ).fetchone()
    if col:
        return

    # 2) ajouter la colonne
    db.session.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {column} {coltype_sql}"))
    db.session.commit()

# ---------- Ingestor Infoclimat : Roissy–Charles-de-Gaulle (07157) ----------
IC_CDG_URL = "https://www.infoclimat.fr/observations-meteo/temps-reel/roissy-charles-de-gaulle/07157.html"
PARIS_TZ = ZoneInfo("Europe/Paris")

def _parse_ic_cdg_humidity_rows(html: str):
    soup = BeautifulSoup(html, "html.parser")
    # Try to find a table that contains Humidité rows
    for t in soup.find_all("table"):
        header = " ".join(t.get_text(" ", strip=True).split())
        if ("Humidité" in header) or ("Humi" in header):
            rows = []
            now_paris = datetime.now(PARIS_TZ)
            for tr in t.find_all("tr"):
                txt = " ".join(tr.get_text(" ", strip=True).split())
                if not txt:
                    continue
                m_time = re.search(r"\b([01]?\d|2[0-3])(?:[:h]([0-5]\d))?\b", txt)
                m_hum  = re.search(r"\b(\d{1,3})\s*%\b", txt)
                if not m_time or not m_hum:
                    continue
                hour   = int(m_time.group(1))
                minute = int(m_time.group(2)) if m_time.group(2) else 0
                hum    = int(m_hum.group(1))
                dt_paris = now_paris.replace(hour=hour, minute=minute, second=0, microsecond=0)
                if dt_paris > now_paris:
                    dt_paris = dt_paris - timedelta(days=1)
                rows.append((dt_paris, hum))
            if rows:
                return rows
    # Fallback: scan text
    all_txt = " ".join(soup.get_text(" ", strip=True).split())
    rows, now_paris = [], datetime.now(PARIS_TZ)
    for m in re.finditer(r"\b([01]?\d|2[0-3])(?:[:h]([0-5]\d))?\b[^%]{0,50}?(\d{1,3})\s?%", all_txt):
        hour   = int(m.group(1))
        minute = int(m.group(2)) if m.group(2) else 0
        hum    = int(m.group(3))
        dt_paris = now_paris.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if dt_paris > now_paris:
            dt_paris = dt_paris - timedelta(days=1)
        rows.append((dt_paris, hum))
    return rows

def ingest_infoclimat_cdg(station_id="cdg_07157") -> int:
    r = requests.get(IC_CDG_URL, timeout=12)
    r.raise_for_status()
    pairs = _parse_ic_cdg_humidity_rows(r.text)
    if not pairs:
        return 0
    inserted = 0
    with app.app_context():
        for dt_paris, hum in pairs:
            dt_utc = dt_paris.astimezone(timezone.utc)
            exists = (HumidityObservation.query
                      .filter_by(station_id=station_id)
                      .filter(HumidityObservation.obs_time == dt_utc)
                      .first())
            if exists:
                continue
            db.session.add(HumidityObservation(
                station_id=station_id,
                obs_time=dt_utc,
                humidity=float(hum)
            ))
            inserted += 1
        if inserted:
            db.session.commit()
    return inserted

@app.route("/admin/ingest/cdg")
@login_required
def admin_ingest_cdg():
    try:
        n = ingest_infoclimat_cdg(station_id="cdg_07157")
        return jsonify({"inserted": n})
    except Exception as e:
        app.logger.exception("admin_ingest_cdg failed")
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    with app.app_context():
        # 1) créer toutes les tables connues des modèles
        "db.create_all()"

        # BetListing : colonnes nécessaires
        try:
            tbl = BetListing.__table__.name
            ensure_column(tbl, "kind",           "TEXT")
            ensure_column(tbl, "city",           "TEXT")
            ensure_column(tbl, "date_label",     "TEXT")
            ensure_column(tbl, "deadline_key",   "TEXT")
            ensure_column(tbl, "choice",         "TEXT")
            ensure_column(tbl, "side",           "TEXT NOT NULL DEFAULT 'RAIN'")  # <— IMPORTANT
            ensure_column(tbl, "stake",          "REAL")
            ensure_column(tbl, "base_odds",      "REAL")
            ensure_column(tbl, "boosts_count",   "INTEGER")
            ensure_column(tbl, "boosts_add",     "REAL")
            ensure_column(tbl, "total_odds",     "REAL")
            ensure_column(tbl, "potential_gain", "REAL")
            ensure_column(tbl, "ask_price",       "REAL")
            # si payload était TEXT chez toi, laisse tomber cette ligne
            # ensure_column(tbl, "payload", "TEXT")
            # NB: expires_at est déjà NOT NULL dans ton schéma → on ne le modifie pas ici.
        except Exception as e:
            app.logger.warning(f"[migrate] bet_listing extra cols: {e}")

        # PPPBet : verrouillage trade
        try:
            ensure_column(PPPBet.__table__.name, "locked_for_trade", "INTEGER DEFAULT 0")
        except Exception as e:
            app.logger.warning(f"[migrate] ppp_bet.locked_for_trade: {e}")
        try:
            ensure_column(BetListing.__table__.name, "price", "REAL")
        except Exception as e:
            app.logger.warning(f"[migrate] bet_listing.price: {e}")
        try:
            ensure_column(User.__table__.name, "last_seen", "TIMESTAMP")  # en UTC
        except Exception as e:
            app.logger.warning(f"[migrate] users.last_seen: {e}")    

    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), debug=True)
