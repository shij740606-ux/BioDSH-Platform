use keyring::Entry;
use serde::{Deserialize, Serialize};

const SERVICE: &str = "BioDSH Platform";
const ACCOUNT: &str = "desktop-session";

#[derive(Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
struct StoredSession {
    base_url: String,
    api_path: String,
    access_token: String,
    refresh_token: String,
}

fn entry() -> Result<Entry, String> {
    Entry::new(SERVICE, ACCOUNT).map_err(|e| format!("系统安全存储不可用：{e}"))
}

fn load() -> Option<StoredSession> {
    entry()
        .ok()?
        .get_password()
        .ok()
        .and_then(|v| serde_json::from_str(&v).ok())
}

fn save(session: &StoredSession) -> Result<(), String> {
    let value = serde_json::to_string(session).map_err(|e| e.to_string())?;
    entry()?
        .set_password(&value)
        .map_err(|e| format!("保存登录凭据失败：{e}"))
}

fn clear() {
    if let Ok(item) = entry() {
        let _ = item.delete_credential();
    }
}

fn parse(response: ureq::Response) -> Result<serde_json::Value, String> {
    response
        .into_json()
        .map_err(|e| format!("平台响应无效：{e}"))
}

fn discover(configured_url: &str) -> Result<(String, String), String> {
    let base = configured_url.trim().trim_end_matches('/').to_string();
    if base.is_empty() {
        return Err("请先填写平台地址".into());
    }
    let metadata = ureq::get(&format!("{base}/.well-known/biodsh-platform"))
        .timeout(std::time::Duration::from_secs(15))
        .call()
        .map_err(|e| format!("平台发现失败：{e}"))
        .and_then(parse)?;
    let api = metadata
        .pointer("/api_versions/v1")
        .and_then(|v| v.as_str())
        .filter(|v| v.starts_with('/'))
        .ok_or_else(|| "平台没有提供兼容的 v1 API".to_string())?;
    Ok((base, api.to_string()))
}

fn refresh(session: &mut StoredSession) -> Result<(), String> {
    let response = ureq::post(&format!(
        "{}{}{}",
        session.base_url, session.api_path, "/auth/refresh"
    ))
    .timeout(std::time::Duration::from_secs(15))
    .send_json(serde_json::json!({ "refresh_token": session.refresh_token }))
    .map_err(|_| "登录已过期，请重新登录".to_string())
    .and_then(parse)?;
    session.access_token = response["access_token"]
        .as_str()
        .ok_or("平台登录响应无效")?
        .to_string();
    session.refresh_token = response["refresh_token"]
        .as_str()
        .ok_or("平台登录响应无效")?
        .to_string();
    save(session)
}

fn get(session: &StoredSession, resource: &str) -> Result<serde_json::Value, u16> {
    match ureq::get(&format!(
        "{}{}{}",
        session.base_url, session.api_path, resource
    ))
    .set("Authorization", &format!("Bearer {}", session.access_token))
    .timeout(std::time::Duration::from_secs(15))
    .call()
    {
        Ok(response) => parse(response).map_err(|_| 502),
        Err(ureq::Error::Status(code, _)) => Err(code),
        Err(_) => Err(503),
    }
}

fn get_with_refresh(
    session: &mut StoredSession,
    resource: &str,
) -> Result<serde_json::Value, String> {
    match get(session, resource) {
        Ok(value) => Ok(value),
        Err(401) => {
            refresh(session)?;
            get(session, resource).map_err(|code| format!("平台连接失败：HTTP {code}"))
        }
        Err(code) => Err(format!("平台连接失败：HTTP {code}")),
    }
}

pub fn login(
    configured_url: &str,
    username: &str,
    password: &str,
) -> Result<serde_json::Value, String> {
    let (base_url, api_path) = discover(configured_url)?;
    let response = ureq::post(&format!("{base_url}{api_path}/auth/login"))
        .timeout(std::time::Duration::from_secs(15))
        .send_json(serde_json::json!({ "username": username, "password": password }))
        .map_err(|e| match e {
            ureq::Error::Status(401, _) => "用户名或密码错误".into(),
            _ => format!("登录失败：{e}"),
        })
        .and_then(parse)?;
    let session = StoredSession {
        base_url,
        api_path,
        access_token: response["access_token"]
            .as_str()
            .ok_or("平台登录响应无效")?
            .to_string(),
        refresh_token: response["refresh_token"]
            .as_str()
            .ok_or("平台登录响应无效")?
            .to_string(),
    };
    save(&session)?;
    status(configured_url)
}

pub fn status(configured_url: &str) -> Result<serde_json::Value, String> {
    let configured = configured_url.trim().trim_end_matches('/');
    let Some(mut session) = load() else {
        return Ok(serde_json::json!({ "authenticated": false, "projects": [] }));
    };
    if session.base_url != configured {
        clear();
        return Ok(serde_json::json!({ "authenticated": false, "projects": [] }));
    }
    let me = match get_with_refresh(&mut session, "/auth/me") {
        Ok(value) => value,
        Err(error) if error.contains("登录已过期") => {
            clear();
            return Ok(serde_json::json!({ "authenticated": false, "projects": [] }));
        }
        Err(error) => return Err(error),
    };
    let projects = get_with_refresh(&mut session, "/projects")?;
    Ok(
        serde_json::json!({ "authenticated": true, "user": { "id": me["id"], "username": me["username"], "isActive": me["is_active"], "isAdmin": me["is_admin"] }, "projects": projects }),
    )
}

pub fn logout(configured_url: &str) -> serde_json::Value {
    if let Some(session) = load() {
        if session.base_url == configured_url.trim().trim_end_matches('/') {
            let _ = ureq::post(&format!(
                "{}{}{}",
                session.base_url, session.api_path, "/auth/logout"
            ))
            .timeout(std::time::Duration::from_secs(5))
            .send_json(serde_json::json!({ "refresh_token": session.refresh_token }));
        }
    }
    clear();
    serde_json::json!({ "authenticated": false, "projects": [] })
}
