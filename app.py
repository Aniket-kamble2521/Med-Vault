from app_pkg import create_app


if __name__ == "__main__":
    app = create_app()
    # Allow connections from any device on the network
    app.run(debug=True, host='0.0.0.0', port=5000)
