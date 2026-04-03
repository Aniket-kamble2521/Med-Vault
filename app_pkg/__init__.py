import os
import secrets

from flask import Flask

from .db import close_db, init_db
from .extensions import login_manager


def create_app() -> Flask:
    project_root = os.path.dirname(os.path.dirname(__file__))
    app = Flask(
        __name__,
        instance_relative_config=True,
        template_folder=os.path.join(project_root, "templates"),
        static_folder=os.path.join(project_root, "static"),
        static_url_path="/static",
    )
    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", secrets.token_hex(32))
    app.config["DATABASE"] = os.path.join(app.instance_path, "app.db")
    app.config["SCHEMA_PATH"] = os.path.join(project_root, "schema.sql")
    app.config["UPLOAD_FOLDER"] = os.path.join(app.instance_path, "uploads")
    app.config["QR_FOLDER"] = os.path.join(app.instance_path, "qr")
    app.config["EMERGENCY_TOKEN_TTL_SECONDS"] = 2 * 60 * 60  # 2 hours
    app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024

    os.makedirs(app.instance_path, exist_ok=True)
    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
    os.makedirs(app.config["QR_FOLDER"], exist_ok=True)

    login_manager.login_view = "auth.login"
    login_manager.init_app(app)
    app.teardown_appcontext(close_db)

    with app.app_context():
        init_db()

    from .routes.auth import auth_bp
    from .routes.core import core_bp
    from .routes.emergency import emergency_bp
    from .routes.modules import modules_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(core_bp)
    app.register_blueprint(emergency_bp)
    app.register_blueprint(modules_bp, url_prefix="/modules")
    return app

