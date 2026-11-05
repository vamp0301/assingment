import os
from datetime import datetime, timezone
from urllib.parse import quote_plus

from bson.errors import InvalidDocument
from dotenv import load_dotenv
from flask import Flask, jsonify, request
from pymongo import MongoClient
from pymongo.errors import PyMongoError

# No-op inside the container: .env is never copied into the image.
load_dotenv()

app = Flask(__name__)


def build_mongo_uri():
    """Return the MongoDB URI without ever logging it.

    Kubernetes sets MONGODB_HOST plus credentials from a Secret, and the URI is
    assembled here so special characters in the password are escaped safely.
    Local development sets MONGODB_URI directly.
    """
    host = os.getenv("MONGODB_HOST")
    if not host:
        uri = os.getenv("MONGODB_URI")
        if not uri:
            raise RuntimeError("Set either MONGODB_HOST or MONGODB_URI")
        return uri

    username = os.getenv("MONGODB_USERNAME")
    password = os.getenv("MONGODB_PASSWORD")
    if not username or not password:
        raise RuntimeError(
            "MONGODB_USERNAME and MONGODB_PASSWORD are required with MONGODB_HOST"
        )

    port = os.getenv("MONGODB_PORT", "27017")
    auth_source = os.getenv("MONGODB_AUTH_SOURCE", "admin")
    return (
        f"mongodb://{quote_plus(username)}:{quote_plus(password)}"
        f"@{host}:{port}/?authSource={quote_plus(auth_source)}"
    )


DATABASE_NAME = os.getenv("MONGODB_DATABASE", "flask_db")
COLLECTION_NAME = os.getenv("MONGODB_COLLECTION", "data")

client = MongoClient(build_mongo_uri(), serverSelectionTimeoutMS=5000)
collection = client[DATABASE_NAME][COLLECTION_NAME]


@app.route("/", methods=["GET"])
def index():
    return (
        f"Welcome to the Flask app! The current time is: "
        f"{datetime.now(timezone.utc).isoformat()}"
    ), 200


@app.route("/livez", methods=["GET"])
def livez():
    """Liveness: the process is serving. Deliberately ignores the database,
    so a MongoDB outage does not make Kubernetes restart every Flask pod."""
    return jsonify({"status": "alive"}), 200


@app.route("/health", methods=["GET"])
def health():
    """Readiness: only route traffic here if MongoDB is reachable and auth works."""
    try:
        client.admin.command("ping")
        return jsonify({"status": "healthy", "database": "connected"}), 200
    except PyMongoError:
        app.logger.warning("Health check could not reach MongoDB")
        return jsonify({"status": "unhealthy"}), 503


@app.route("/data", methods=["GET", "POST"])
def data():
    try:
        if request.method == "POST":
            payload = request.get_json(silent=True)

            if not isinstance(payload, dict) or not payload:
                return jsonify({
                    "error": "A non-empty JSON object is required"
                }), 400

            collection.insert_one(payload)

            return jsonify({"status": "Data inserted"}), 201

        documents = list(collection.find({}, {"_id": 0}))
        return jsonify(documents), 200

    except InvalidDocument:
        return jsonify({"error": "Document contains invalid field names"}), 400
    except PyMongoError:
        # Log the failure type only: full tracebacks can include connection details.
        app.logger.error("Database operation failed")
        return jsonify({"error": "Database operation failed"}), 503


if __name__ == "__main__":
    app.run(
        host=os.getenv("FLASK_HOST", "0.0.0.0"),
        port=int(os.getenv("FLASK_PORT", "5050")),
        debug=os.getenv("FLASK_ENV") == "development"
    )
