use keyring::Entry;
use serde::{Deserialize, Serialize};
use std::fs;
use std::io;
use std::path::Path;

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

fn patch(
    session: &StoredSession,
    resource: &str,
    body: serde_json::Value,
) -> Result<serde_json::Value, u16> {
    match ureq::patch(&format!(
        "{}{}{}",
        session.base_url, session.api_path, resource
    ))
    .set("Authorization", &format!("Bearer {}", session.access_token))
    .timeout(std::time::Duration::from_secs(15))
    .send_json(body)
    {
        Ok(response) => parse(response).map_err(|_| 502),
        Err(ureq::Error::Status(code, _)) => Err(code),
        Err(_) => Err(503),
    }
}

fn post(
    session: &StoredSession,
    resource: &str,
    body: serde_json::Value,
) -> Result<serde_json::Value, u16> {
    match ureq::post(&format!(
        "{}{}{}",
        session.base_url, session.api_path, resource
    ))
    .set("Authorization", &format!("Bearer {}", session.access_token))
    .timeout(std::time::Duration::from_secs(15))
    .send_json(body)
    {
        Ok(response) => parse(response).map_err(|_| 502),
        Err(ureq::Error::Status(code, _)) => Err(code),
        Err(_) => Err(503),
    }
}

fn post_with_refresh(
    session: &mut StoredSession,
    resource: &str,
    body: serde_json::Value,
) -> Result<serde_json::Value, String> {
    match post(session, resource, body.clone()) {
        Ok(value) => Ok(value),
        Err(401) => {
            refresh(session)?;
            post(session, resource, body).map_err(|code| format!("平台连接失败：HTTP {code}"))
        }
        Err(code) => Err(format!("平台连接失败：HTTP {code}")),
    }
}

fn configured_session(configured_url: &str) -> Result<StoredSession, String> {
    let configured = configured_url.trim().trim_end_matches('/');
    let session = load().ok_or_else(|| "请先登录平台".to_string())?;
    if session.base_url != configured {
        clear();
        return Err("请先登录平台".into());
    }
    Ok(session)
}

fn normalize_metric(value: &serde_json::Value) -> serde_json::Value {
    serde_json::json!({
        "id": value["id"], "nodeId": value["node_id"],
        "cpuPercent": value["cpu_percent"], "loadOne": value["load_one"],
        "memoryUsedBytes": value["memory_used_bytes"], "memoryTotalBytes": value["memory_total_bytes"],
        "memoryPercent": value["memory_percent"], "diskUsedBytes": value["disk_used_bytes"],
        "diskTotalBytes": value["disk_total_bytes"], "diskPercent": value["disk_percent"],
        "networkRxBytes": value["network_rx_bytes"], "networkTxBytes": value["network_tx_bytes"],
        "runningJobs": value["running_jobs"], "gpus": value["gpus"], "collectedAt": value["collected_at"]
    })
}

fn normalize_node(value: &serde_json::Value) -> serde_json::Value {
    let metric = if value["latest_metric"].is_null() {
        serde_json::Value::Null
    } else {
        normalize_metric(&value["latest_metric"])
    };
    serde_json::json!({
        "id": value["id"], "name": value["name"], "status": value["status"],
        "hostname": value["hostname"], "operatingSystem": value["operating_system"],
        "agentVersion": value["agent_version"], "capabilities": value["capabilities"],
        "latestMetric": metric, "lastHeartbeatAt": value["last_heartbeat_at"]
    })
}

fn normalize_job(value: &serde_json::Value) -> serde_json::Value {
    serde_json::json!({
        "id": value["id"], "projectId": value["project_id"], "nodeId": value["node_id"],
        "kind": value["kind"], "status": value["status"], "payload": value["payload"],
        "requestedResources": value["requested_resources"], "priority": value["priority"],
        "attempt": value["attempt"], "assignedAt": value["assigned_at"],
        "startedAt": value["started_at"], "finishedAt": value["finished_at"],
        "exitCode": value["exit_code"], "resultManifest": value["result_manifest"],
        "errorMessage": value["error_message"], "createdAt": value["created_at"],
        "updatedAt": value["updated_at"]
    })
}

fn normalize_job_event(value: &serde_json::Value) -> serde_json::Value {
    serde_json::json!({
        "id": value["id"], "jobId": value["job_id"], "sequence": value["sequence"],
        "kind": value["kind"], "message": value["message"], "payload": value["payload"],
        "createdAt": value["created_at"]
    })
}

fn normalize_artifact(value: &serde_json::Value) -> serde_json::Value {
    serde_json::json!({
        "id": value["id"], "jobId": value["job_id"], "relativePath": value["relative_path"],
        "sizeBytes": value["size_bytes"], "sha256": value["sha256"],
        "contentType": value["content_type"], "createdAt": value["created_at"]
    })
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

pub fn nodes(configured_url: &str) -> Result<serde_json::Value, String> {
    let mut session = configured_session(configured_url)?;
    let values = get_with_refresh(&mut session, "/nodes")?;
    let rows = values
        .as_array()
        .ok_or_else(|| "服务器列表响应无效".to_string())?;
    Ok(serde_json::Value::Array(
        rows.iter().map(normalize_node).collect(),
    ))
}

pub fn node_metrics(
    configured_url: &str,
    node_id: &str,
    minutes: u32,
) -> Result<serde_json::Value, String> {
    let mut session = configured_session(configured_url)?;
    let resource = format!("/nodes/{node_id}/metrics?minutes={}", minutes.max(1));
    let values = get_with_refresh(&mut session, &resource)?;
    let rows = values
        .as_array()
        .ok_or_else(|| "资源历史响应无效".to_string())?;
    Ok(serde_json::Value::Array(
        rows.iter().map(normalize_metric).collect(),
    ))
}

pub fn update_node(
    configured_url: &str,
    node_id: &str,
    status: &str,
) -> Result<serde_json::Value, String> {
    if !matches!(status, "online" | "draining" | "disabled") {
        return Err("不支持的服务器状态".into());
    }
    let mut session = configured_session(configured_url)?;
    let resource = format!("/nodes/{node_id}");
    let body = serde_json::json!({ "status": status });
    let value = match patch(&session, &resource, body.clone()) {
        Ok(value) => value,
        Err(401) => {
            refresh(&mut session)?;
            patch(&session, &resource, body)
                .map_err(|code| format!("服务器状态更新失败：HTTP {code}"))?
        }
        Err(code) => return Err(format!("服务器状态更新失败：HTTP {code}")),
    };
    Ok(normalize_node(&value))
}

pub fn jobs(configured_url: &str, project_id: &str) -> Result<serde_json::Value, String> {
    let mut session = configured_session(configured_url)?;
    let resource = format!("/jobs?project_id={project_id}");
    let values = get_with_refresh(&mut session, &resource)?;
    let rows = values.as_array().ok_or("任务列表响应无效")?;
    Ok(serde_json::Value::Array(
        rows.iter().map(normalize_job).collect(),
    ))
}

pub fn job_kinds(configured_url: &str) -> Result<serde_json::Value, String> {
    let mut session = configured_session(configured_url)?;
    get_with_refresh(&mut session, "/job-kinds")
}

pub fn create_job(
    configured_url: &str,
    input: serde_json::Value,
) -> Result<serde_json::Value, String> {
    let mut session = configured_session(configured_url)?;
    let body = serde_json::json!({
        "project_id": input["projectId"], "kind": input["kind"],
        "payload": { "arguments": input["arguments"] },
        "requested_resources": input["requestedResources"],
        "priority": input.get("priority").cloned().unwrap_or(serde_json::json!(0))
    });
    Ok(normalize_job(&post_with_refresh(
        &mut session,
        "/jobs",
        body,
    )?))
}

pub fn cancel_job(configured_url: &str, job_id: &str) -> Result<serde_json::Value, String> {
    let mut session = configured_session(configured_url)?;
    let value = post_with_refresh(
        &mut session,
        &format!("/jobs/{job_id}/cancel"),
        serde_json::json!({}),
    )?;
    Ok(normalize_job(&value))
}

pub fn job_events(configured_url: &str, job_id: &str) -> Result<serde_json::Value, String> {
    let mut session = configured_session(configured_url)?;
    let values = get_with_refresh(&mut session, &format!("/jobs/{job_id}/events"))?;
    let rows = values.as_array().ok_or("任务日志响应无效")?;
    Ok(serde_json::Value::Array(
        rows.iter().map(normalize_job_event).collect(),
    ))
}

pub fn job_artifacts(configured_url: &str, job_id: &str) -> Result<serde_json::Value, String> {
    let mut session = configured_session(configured_url)?;
    let values = get_with_refresh(&mut session, &format!("/jobs/{job_id}/artifacts"))?;
    let rows = values.as_array().ok_or("任务结果响应无效")?;
    Ok(serde_json::Value::Array(
        rows.iter().map(normalize_artifact).collect(),
    ))
}

pub fn download_job_artifact(
    configured_url: &str,
    job_id: &str,
    artifact_id: &str,
    target: &Path,
) -> Result<(), String> {
    let mut session = configured_session(configured_url)?;
    let resource = format!("/jobs/{job_id}/artifacts/{artifact_id}");
    for attempt in 0..2 {
        let response = ureq::get(&format!(
            "{}{}{}",
            session.base_url, session.api_path, resource
        ))
        .set("Authorization", &format!("Bearer {}", session.access_token))
        .timeout(std::time::Duration::from_secs(120))
        .call();
        match response {
            Ok(response) => {
                if let Some(parent) = target.parent() {
                    fs::create_dir_all(parent).map_err(|e| e.to_string())?;
                }
                let temporary = target.with_extension("part");
                let mut output = fs::File::create(&temporary).map_err(|e| e.to_string())?;
                if let Err(error) = io::copy(&mut response.into_reader(), &mut output) {
                    let _ = fs::remove_file(&temporary);
                    return Err(error.to_string());
                }
                if target.exists() {
                    fs::remove_file(target).map_err(|e| e.to_string())?;
                }
                fs::rename(&temporary, target).map_err(|e| e.to_string())?;
                return Ok(());
            }
            Err(ureq::Error::Status(401, _)) if attempt == 0 => refresh(&mut session)?,
            Err(ureq::Error::Status(code, _)) => {
                return Err(format!("结果下载失败：HTTP {code}"));
            }
            Err(_) => return Err("结果下载失败：网络连接不可用".into()),
        }
    }
    Err("结果下载失败".into())
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
