from app_pkg import create_app


if __name__ == "__main__":
    app = create_app()
    # Allow connections from any device on the network
    import os

port = int(os.environ.get("PORT", 5000))
app.run(host="0.0.0.0", port=port)
