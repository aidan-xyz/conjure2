from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
from supabase import create_client, Client
from dotenv import load_dotenv
from functools import wraps
import os
import json
import threading
import csv
import io

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev")

# Anon client — used for auth validation only.
supabase: Client = create_client(
    os.environ.get("SUPABASE_URL", ""),
    os.environ.get("SUPABASE_PUBLISHABLE_KEY", ""),
)

# Service-role client — used for all DB reads/writes.
# Bypasses RLS; always filter by user_id manually.
_service_key = os.environ.get("SUPABASE_SECRET_KEY", "")
service_db: Client = create_client(os.environ.get("SUPABASE_URL", ""), _service_key) if _service_key else supabase


# ---------------------------------------------------------------------------
# Background enrichment worker
# ---------------------------------------------------------------------------

def _enrich_lead_worker(lead_id: str, name: str, linkedin_url, openai_key: str):
    """Runs in a daemon thread: enrich one lead and save raw_data to Supabase."""
    from pipeline import enrich_lead
    try:
        service_db.table("leads").update({"status": "processing"}).eq("id", lead_id).execute()
        raw_data = enrich_lead(name, linkedin_url, openai_key)
        service_db.table("leads").update({
            "raw_data": raw_data,
            "status": "enriched",
        }).eq("id", lead_id).execute()
    except Exception as e:
        service_db.table("leads").update({
            "status": "failed",
            "error_message": str(e)[:500],
        }).eq("id", lead_id).execute()


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
    settings = session.get("user_settings", {})
    try:
        leads = service_db.table("leads") \
            .select("id,name,email,linkedin_url,status,error_message") \
            .eq("user_id", user.id) \
            .order("created_at", desc=True) \
            .execute().data
    except Exception:
        leads = []
    return render_template("dashboard.html", user=user, settings=settings, leads=leads, result=None, gen_error=None)


@app.route("/leads/import", methods=["POST"])
@login_required
def import_leads():
    data = request.get_json()
    if not data:
        return jsonify({"error": "No data"}), 400

    csv_text = data.get("csvText", "")
    mapping = data.get("mapping", {})

    settings = session.get("user_settings", {})
    openai_key = settings.get("openai_key") or os.environ.get("OPENAI_API_KEY", "")

    access_token = session.get("access_token")
    user = supabase.auth.get_user(jwt=access_token).user

    reader = csv.DictReader(io.StringIO(csv_text))
    lead_ids = []

    for row in reader:
        lead_data = {
            "user_id": user.id,
            "name": row.get(mapping.get("name", ""), "").strip(),
            "email": row.get(mapping.get("email", ""), "").strip(),
            "linkedin_url": row.get(mapping.get("linkedin_url", ""), "").strip() or None,
            "address": row.get(mapping.get("address", ""), "").strip() or None,
            "status": "pending",
        }
        result = service_db.table("leads").insert(lead_data).execute()
        lead_id = result.data[0]["id"]
        lead_ids.append(lead_id)

        if openai_key:
            t = threading.Thread(
                target=_enrich_lead_worker,
                args=(lead_id, lead_data["name"], lead_data["linkedin_url"], openai_key),
                daemon=True,
            )
            t.start()

    return jsonify({"imported": len(lead_ids), "lead_ids": lead_ids})


@app.route("/leads/status")
@login_required
def leads_status():
    access_token = session.get("access_token")
    user = supabase.auth.get_user(jwt=access_token).user
    leads = service_db.table("leads") \
        .select("id,name,email,linkedin_url,status,error_message") \
        .eq("user_id", user.id) \
        .order("created_at", desc=True) \
        .execute().data
    return jsonify(leads)


@app.route("/leads/<lead_id>/generate", methods=["POST"])
@login_required
def generate_piece(lead_id):
    from pipeline import render_template as render_tmpl
    access_token = session.get("access_token")
    user = supabase.auth.get_user(jwt=access_token).user
    settings = session.get("user_settings", {})
    openai_key = settings.get("openai_key") or os.environ.get("OPENAI_API_KEY", "")

    if not openai_key:
        return jsonify({"error": "No OpenAI key configured"}), 400

    resp = service_db.table("leads").select("*") \
        .eq("id", lead_id).eq("user_id", user.id) \
        .maybe_single().execute()
    if not resp.data:
        return jsonify({"error": "Lead not found"}), 404
    lead = resp.data

    if lead["status"] != "enriched":
        return jsonify({"error": f"Lead is '{lead['status']}' — must be 'enriched' before generating"}), 400

    body = request.get_json(force=True, silent=True) or {}
    template_name = body.get("template", "homepage")

    try:
        output = render_tmpl(lead["raw_data"], template_name, openai_key)
        service_db.table("pieces").insert({
            "user_id": user.id,
            "lead_id": lead_id,
            "template": template_name,
            "output_json": output,
            "status": "ready",
        }).execute()
        return jsonify({"output": output})
    except Exception as e:
        return jsonify({"error": str(e)}), 500



@login_required
def save_keys():
    settings = session.get("user_settings", {})
    # Only overwrite if the user typed a real value (not the masked placeholder)
    for field in ("orangeslice_key", "openai_key"):
        val = request.form.get(field, "").strip()
        if val and not set(val) <= {"•"}:
            settings[field] = val
    session["user_settings"] = settings
    flash("API keys saved.", "success")
    return redirect(url_for("dashboard") + "#settings")


@app.route("/settings/sender", methods=["POST"])
@login_required
def save_sender():
    settings = session.get("user_settings", {})
    for field in ("sender_name", "company", "return_address"):
        settings[field] = request.form.get(field, "").strip()
    session["user_settings"] = settings
    flash("Sender details saved.", "success")
    return redirect(url_for("dashboard") + "#settings")


@app.route("/generate", methods=["POST"])
@login_required
def generate():
    from pipeline import run_pipeline
    settings = session.get("user_settings", {})
    openai_key = settings.get("openai_key") or os.environ.get("OPENAI_API_KEY", "")
    name = request.form.get("name", "").strip()
    linkedin_url = request.form.get("linkedin_url", "").strip() or None

    if not openai_key:
        access_token = session.get("access_token")
        user = supabase.auth.get_user(jwt=access_token).user
        return render_template("dashboard.html", user=user, settings=settings,
                               result=None, gen_error="Add your OpenAI API key in Settings first.")

    try:
        result = run_pipeline(name, linkedin_url, openai_key)
        access_token = session.get("access_token")
        user = supabase.auth.get_user(jwt=access_token).user
        return render_template("dashboard.html", user=user, settings=settings,
                               result=json.dumps(result, indent=2), gen_error=None)
    except Exception as e:
        access_token = session.get("access_token")
        user = supabase.auth.get_user(jwt=access_token).user
        return render_template("dashboard.html", user=user, settings=settings,
                               result=None, gen_error=str(e))


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
