from flask import Flask, render_template, request, redirect, url_for, session, flash
from supabase import create_client, Client
from dotenv import load_dotenv
from functools import wraps
import os

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev")

# Use the publishable key for auth and user-facing operations.
# Use the secret key (SUPABASE_SECRET_KEY) only for admin/server operations
# that need to bypass RLS — never expose it to the client.
supabase: Client = create_client(
    os.environ.get("SUPABASE_URL", ""),
    os.environ.get("SUPABASE_PUBLISHABLE_KEY", ""),
)


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        access_token = session.get("access_token")
        if not access_token:
            return redirect(url_for("auth"))
        try:
            response = supabase.auth.get_user(jwt=access_token)
            if not response.user:
                session.clear()
                return redirect(url_for("auth"))
        except Exception:
            session.clear()
            return redirect(url_for("auth"))
        return f(*args, **kwargs)
    return decorated


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/auth", methods=["GET", "POST"])
def auth():
    if session.get("access_token"):
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        action = request.form.get("action")
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "").strip()

        if action == "login":
            try:
                response = supabase.auth.sign_in_with_password(
                    {"email": email, "password": password}
                )
                session["access_token"] = response.session.access_token
                session["refresh_token"] = response.session.refresh_token
                return redirect(url_for("dashboard"))
            except Exception as e:
                flash(str(e), "error")

        elif action == "signup":
            confirm = request.form.get("confirm_password", "").strip()
            if password != confirm:
                flash("Passwords do not match.", "error")
            else:
                try:
                    supabase.auth.sign_up({"email": email, "password": password})
                    flash("Check your email to confirm your account.", "success")
                except Exception as e:
                    flash(str(e), "error")

    return render_template("auth.html")


@app.route("/dashboard")
@login_required
def dashboard():
    access_token = session.get("access_token")
    user = supabase.auth.get_user(jwt=access_token).user
    return render_template("dashboard.html", user=user)


@app.route("/logout", methods=["POST"])
def logout():
    try:
        supabase.auth.sign_out()
    except Exception:
        pass
    session.clear()
    return redirect(url_for("index"))


if __name__ == "__main__":
    app.run(debug=os.environ.get("FLASK_DEBUG", "0") == "1")
