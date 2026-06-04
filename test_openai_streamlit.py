import os
import json
import traceback
import streamlit as st
from openai import OpenAI


def mask_value(value):
    if not value:
        return "<missing>"
    value = str(value).strip()
    if len(value) <= 8:
        return "*" * len(value)
    return value[:4] + "..." + value[-4:]


def normalize_api_key(key_value):
    if not key_value:
        return None
    key_value = str(key_value).strip()
    if key_value.startswith("OPENAI_API_KEY") and "=" in key_value:
        key_value = key_value.split("=", 1)[1].strip()
    if (key_value.startswith('"') and key_value.endswith('"')) or (key_value.startswith("'") and key_value.endswith("'")):
        key_value = key_value[1:-1].strip()
    return key_value or None


def get_openai_api_key():
    env_key = normalize_api_key(os.getenv("OPENAI_API_KEY"))
    if env_key:
        return env_key, "environment"

    try:
        secret_key = normalize_api_key(st.secrets.get("OPENAI_API_KEY"))
    except Exception:
        secret_key = None
    if secret_key:
        return secret_key, "streamlit_secrets"

    return None, None


def log_section(title, message):
    st.markdown(f"### {title}")
    st.code(message)
    print(f"{title}\n{message}\n")


def get_runtime_info():
    data = {
        "python_executable": sys.executable,
        "cwd": os.getcwd(),
        "github_actions": bool(os.getenv("GITHUB_ACTIONS")),
        "git_ref": os.getenv("GITHUB_REF"),
        "github_repository": os.getenv("GITHUB_REPOSITORY"),
        "github_run_id": os.getenv("GITHUB_RUN_ID"),
        "github_job": os.getenv("GITHUB_JOB"),
        "ci": bool(os.getenv("CI")),
    }
    return data


def run_openai_check(key, source):
    st.info(f"OpenAI-Key wird aus {source} verwendet.")
    log_section("Selected API Key", f"source={source}\nmasked={mask_value(key)}\nlength={len(key)}")

    try:
        client = OpenAI(api_key=key)
        response = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[{"role": "user", "content": "Teste die Verbindung zur OpenAI API."}],
            temperature=0.0,
            max_tokens=3,
        )

        if hasattr(response, "choices") and len(response.choices) > 0:
            choice = response.choices[0]
            content = getattr(choice, "message", {}).get("content") if hasattr(choice, "message") else None
            if not content:
                content = str(choice)
        else:
            content = str(response)

        log_section("OpenAI API Antwort", json.dumps({
            "response_type": type(response).__name__,
            "choices": content,
        }, indent=2, default=str))
        st.success("OpenAI API-Aufruf war erfolgreich.")
    except Exception as err:
        st.error("OpenAI API-Aufruf ist fehlgeschlagen.")
        log_section("OpenAI Exception", traceback.format_exc())
        st.markdown("**Fehler-Details:**")
        st.error(str(err))


if __name__ == "__main__":
    import sys

    st.set_page_config(page_title="OpenAI Streamlit Debugger", layout="wide")
    st.title("🔧 OpenAI / Streamlit Debug Script")
    st.write(
        "Dieses Debug-Script prüft, ob der OpenAI-API-Key in der Umgebung oder in Streamlit-Secrets vorhanden ist, und testet einen einfachen API-Aufruf."
    )

    runtime_info = {
        "python_executable": sys.executable,
        "cwd": os.getcwd(),
        "githhub_actions_detected": bool(os.getenv("GITHUB_ACTIONS")),
        "ci_detected": bool(os.getenv("CI")),
        "github_repository": os.getenv("GITHUB_REPOSITORY"),
        "github_ref": os.getenv("GITHUB_REF"),
        "github_run_id": os.getenv("GITHUB_RUN_ID"),
        "github_job": os.getenv("GITHUB_JOB"),
    }
    log_section("Runtime-Informationen", json.dumps(runtime_info, indent=2))

    env_value = os.getenv("OPENAI_API_KEY")
    log_section(
        "OPENAI_API_KEY aus Environment",
        json.dumps({
            "present": bool(env_value),
            "masked": mask_value(env_value),
            "length": len(env_value or ""),
        }, indent=2),
    )

    streamlit_secret_value = None
    try:
        streamlit_secret_value = st.secrets.get("OPENAI_API_KEY")
    except Exception as secret_err:
        st.error("Fehler beim Lesen von Streamlit-Secrets.")
        log_section("Streamlit Secrets Fehler", traceback.format_exc())

    log_section(
        "OPENAI_API_KEY aus Streamlit-Secrets",
        json.dumps({
            "present": bool(streamlit_secret_value),
            "masked": mask_value(streamlit_secret_value),
            "length": len(str(streamlit_secret_value or "")),
        }, indent=2),
    )

    key, source = get_openai_api_key()
    if not key:
        st.error(
            "Kein gültiger OpenAI API-Key gefunden. Bitte setze `OPENAI_API_KEY` als Umgebungsvariable oder in Streamlit-Secrets."
        )
    else:
        run_openai_check(key, source)
