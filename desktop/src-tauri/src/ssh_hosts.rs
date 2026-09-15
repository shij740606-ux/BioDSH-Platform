use keyring::Entry;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::{fs, io::{BufRead, Write}, path::{Path, PathBuf}, process::{Command, Stdio}, sync::Mutex};

static WRITE_LOCK: Mutex<()> = Mutex::new(());

#[derive(Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct Host {
    pub id: String, pub name: String, pub host: String, pub port: u16, pub username: String,
    pub auth: String, pub selected: bool, pub fingerprint: Option<String>, pub host_key: Option<String>,
}
#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct Draft {
    pub id: Option<String>, pub name: String, pub host: String, pub port: u16, pub username: String,
    pub auth: String, pub password: Option<String>, pub private_key: Option<String>, pub passphrase: Option<String>,
}
#[derive(Default, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
struct Secret { password: String, private_key: String, passphrase: String }
#[derive(Serialize, Deserialize)]
struct SecretParts { revision: String, count: usize }

fn secret_entry(id: &str) -> Result<Entry, String> { Entry::new("BioDSH SSH", id).map_err(|_| "系统安全存储不可用".into()) }
fn secret(id: &str) -> Result<Secret, String> {
    let value = secret_entry(id)?.get_password().map_err(|_| "服务器凭据未保存，请编辑服务器重新填写".to_string())?;
    if let Ok(parts) = serde_json::from_str::<SecretParts>(&value) {
        if parts.count > 512 { return Err("服务器凭据无效".into()); }
        let mut payload = String::new();
        for index in 0..parts.count {
            payload.push_str(&secret_entry(&format!("{id}-{}-{index}", parts.revision))?.get_password().map_err(|_| "服务器凭据不完整".to_string())?);
        }
        return serde_json::from_str(&payload).map_err(|_| "服务器凭据无效".into());
    }
    serde_json::from_str(&value).map_err(|_| "服务器凭据无效".into())
}
fn delete_parts(id: &str, record: &str) {
    if let Ok(parts) = serde_json::from_str::<SecretParts>(record) {
        for index in 0..parts.count.min(512) {
            if let Ok(entry) = secret_entry(&format!("{id}-{}-{index}", parts.revision)) { let _ = entry.delete_credential(); }
        }
    }
}
fn save_secret(id: &str, secret: &Secret) -> Result<(), String> {
    // Windows Credential Manager has a per-entry size limit; split large private keys without writing plaintext files.
    let payload = serde_json::to_string(secret).map_err(|e| e.to_string())?;
    if payload.len() > 262144 { return Err("SSH 凭据过大".into()); }
    let old = secret_entry(id)?.get_password().unwrap_or_default();
    let mut parts = SecretParts { revision: uuid::Uuid::new_v4().simple().to_string(), count: 0 };
    let mut remaining = payload.as_str();
    while !remaining.is_empty() {
        let mut end = remaining.len().min(900); while !remaining.is_char_boundary(end) { end -= 1; }
        let item = secret_entry(&format!("{id}-{}-{}", parts.revision, parts.count))?;
        if item.set_password(&remaining[..end]).is_err() { delete_parts(id, &serde_json::to_string(&parts).unwrap_or_default()); return Err("保存 SSH 凭据失败".into()); }
        parts.count += 1; remaining = &remaining[end..];
    }
    let record = serde_json::to_string(&parts).map_err(|e| e.to_string())?;
    if secret_entry(id)?.set_password(&record).is_err() { delete_parts(id, &record); return Err("保存 SSH 凭据失败".into()); }
    delete_parts(id, &old); Ok(())
}
pub fn list(root: &Path) -> Result<Vec<Host>, String> {
    let file = root.join("ssh/hosts.json");
    if !file.exists() { return Ok(Vec::new()); }
    serde_json::from_str(&fs::read_to_string(file).map_err(|e| e.to_string())?).map_err(|_| "服务器列表文件损坏，请勿覆盖原文件".into())
}
fn persist(root: &Path, hosts: &[Host]) -> Result<(), String> {
    let dir = root.join("ssh"); fs::create_dir_all(&dir).map_err(|e| e.to_string())?;
    let temp = dir.join("hosts.tmp");
    fs::write(&temp, serde_json::to_vec_pretty(hosts).map_err(|e| e.to_string())?).map_err(|e| e.to_string())?;
    fs::rename(temp, dir.join("hosts.json")).map_err(|e| e.to_string())
}
fn validate(draft: &Draft) -> Result<(), String> {
    if draft.name.trim().is_empty() || draft.name.len() > 120 || draft.port == 0 || draft.username.trim().is_empty() || draft.username.len() > 128 {
        return Err("请填写名字、用户名和有效端口".into());
    }
    if draft.host.is_empty() || draft.host.len() > 253 || !draft.host.chars().all(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '-' | ':' | '_')) {
        return Err("请填写有效 IP 或主机名，不包含协议或路径".into());
    }
    if !matches!(draft.auth.as_str(), "password" | "key") { return Err("认证方式无效".into()); }
    Ok(())
}
pub fn save(root: &Path, draft: Draft) -> Result<Vec<Host>, String> {
    validate(&draft)?;
    let _guard = WRITE_LOCK.lock().map_err(|_| "保存锁不可用")?;
    let mut hosts = list(root)?;
    let existing = draft.id.as_ref().and_then(|id| hosts.iter().find(|h| &h.id == id)).cloned();
    if draft.id.is_some() && existing.is_none() { return Err("服务器不存在".into()); }
    let id = existing.as_ref().map(|h| h.id.clone()).unwrap_or_else(|| uuid::Uuid::new_v4().to_string());
    let mut credentials = if existing.is_some() { secret(&id).unwrap_or_default() } else { Secret::default() };
    if let Some(value) = draft.password.filter(|v| !v.is_empty()) { credentials.password = value; }
    if let Some(value) = draft.private_key.filter(|v| !v.trim().is_empty()) { credentials.private_key = value; }
    if let Some(value) = draft.passphrase { credentials.passphrase = value; }
    if (draft.auth == "password" && credentials.password.is_empty()) || (draft.auth == "key" && credentials.private_key.is_empty()) { return Err("请填写所选认证方式的密码或私钥".into()); }
    let same_endpoint = existing.as_ref().is_some_and(|h| h.host == draft.host.trim() && h.port == draft.port);
    let host = Host { id: id.clone(), name: draft.name.trim().into(), host: draft.host.trim().into(), port: draft.port,
        username: draft.username.trim().into(), selected: existing.as_ref().is_some_and(|h| h.selected && h.username == draft.username.trim() && h.auth == draft.auth) && same_endpoint, auth: draft.auth,
        fingerprint: existing.as_ref().filter(|_| same_endpoint).and_then(|h| h.fingerprint.clone()),
        host_key: existing.as_ref().filter(|_| same_endpoint).and_then(|h| h.host_key.clone()) };
    save_secret(&id, &credentials)?;
    hosts.retain(|h| h.id != id); hosts.push(host); persist(root, &hosts)?; Ok(hosts)
}
pub fn remove(root: &Path, id: &str) -> Result<Vec<Host>, String> {
    let _guard = WRITE_LOCK.lock().map_err(|_| "保存锁不可用")?;
    let mut hosts = list(root)?; hosts.retain(|h| h.id != id); persist(root, &hosts)?;
    let entry = secret_entry(id)?;
    if let Ok(record) = entry.get_password() { delete_parts(id, &record); }
    let _ = entry.delete_credential(); Ok(hosts)
}
pub fn select(root: &Path, id: &str, selected: bool) -> Result<Vec<Host>, String> {
    let _guard = WRITE_LOCK.lock().map_err(|_| "保存锁不可用")?;
    let mut hosts = list(root)?; let host = hosts.iter_mut().find(|h| h.id == id).ok_or("服务器不存在")?;
    if selected && host.host_key.is_none() { return Err("请先测试连接并确认主机指纹".into()); }
    host.selected = selected; persist(root, &hosts)?; Ok(hosts)
}
pub fn trust(root: &Path, id: &str, host_key: &str, fingerprint: &str) -> Result<Vec<Host>, String> {
    let _guard = WRITE_LOCK.lock().map_err(|_| "保存锁不可用")?;
    if host_key.len() < 20 || host_key.len() > 16384 || !fingerprint.starts_with("SHA256:") { return Err("主机指纹无效".into()); }
    let mut hosts = list(root)?; let host = hosts.iter_mut().find(|h| h.id == id).ok_or("服务器不存在")?;
    host.host_key = Some(host_key.into()); host.fingerprint = Some(fingerprint.into()); host.selected = false;
    persist(root, &hosts)?; Ok(hosts)
}
pub fn call(root: &Path, node: &Path, script: &Path, id: &str, operation: &str, command: &str, timeout: u64, agent: bool) -> Result<Value, String> {
    let host = list(root)?.into_iter().find(|h| h.id == id).ok_or("服务器不存在")?;
    if agent && !host.selected { return Err("该服务器未被用户选择，禁止连接".into()); }
    if operation != "probe" && host.host_key.is_none() { return Err("请在服务器管理页面确认主机指纹".into()); }
    if command.len() > 32768 || command.contains('\0') { return Err("命令过长或无效".into()); }
    let credentials = if operation == "probe" { Secret::default() } else { secret(id)? };
    let request = json!({"operation": operation, "host": host.host, "port": host.port, "username": host.username,
        "hostKey": host.host_key, "password": if host.auth == "password" { Some(credentials.password) } else { None },
        "privateKey": if host.auth == "key" { Some(credentials.private_key) } else { None },
        "passphrase": credentials.passphrase, "command": command, "timeoutSeconds": timeout.clamp(5, 300)});
    let mut process = Command::new(node); process.arg(script).stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::null());
    #[cfg(windows)] { use std::os::windows::process::CommandExt; process.creation_flags(0x0800_0000); }
    let mut child = process.spawn().map_err(|_| "SSH 运行时不可用，请重新构建或安装完整桌面程序".to_string())?;
    let mut input = child.stdin.take().ok_or("SSH 输入管道不可用")?;
    input.write_all(&serde_json::to_vec(&request).map_err(|e| e.to_string())?).map_err(|e| e.to_string())?; drop(input);
    let output = child.wait_with_output().map_err(|e| e.to_string())?;
    let value: Value = serde_json::from_slice(&output.stdout).map_err(|_| "SSH 运行时返回无效响应".to_string())?;
    if let Some(error) = value.get("error").and_then(Value::as_str) { return Err(error.into()); }
    Ok(value)
}

pub fn run_mcp() {
    let root = PathBuf::from(std::env::var_os("BIODSH_HOME").unwrap_or_default());
    let node = PathBuf::from(std::env::var_os("BIODSH_SSH_NODE").unwrap_or_default());
    let script = PathBuf::from(std::env::var_os("BIODSH_SSH_SCRIPT").unwrap_or_default());
    for line in std::io::stdin().lock().lines().map_while(Result::ok) {
        if line.len() > 262144 { break; }
        let Ok(request) = serde_json::from_str::<Value>(&line) else { continue };
        let Some(id) = request.get("id") else { continue };
        let result: Result<Value, String> = match request["method"].as_str().unwrap_or("") {
            "initialize" => Ok(json!({"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "BioDSH SSH", "version": "1.0.0"}})),
            "ping" => Ok(json!({})),
            "tools/list" => Ok(json!({"tools": [
                {"name":"list_servers","description":"List only SSH servers explicitly selected by the user. Never request or expose credentials.","inputSchema":{"type":"object","properties":{},"additionalProperties":false}},
                {"name":"execute","description":"Run a shell command on a user-selected SSH server. Use list_servers to get the server ID. Credentials are handled locally. Each command has a fresh shell; include cd in the command. Ask the user before destructive actions. Timeout does not guarantee termination of background remote work.","inputSchema":{"type":"object","properties":{"serverId":{"type":"string"},"command":{"type":"string"},"timeoutSeconds":{"type":"integer","minimum":5,"maximum":300}},"required":["serverId","command"],"additionalProperties":false}}
            ]})),
            "tools/call" => {
                let params = &request["params"];
                let value = match params["name"].as_str().unwrap_or("") {
                    "list_servers" => list(&root).map(|hosts| json!(hosts.into_iter().filter(|h| h.selected).collect::<Vec<_>>())),
                    "execute" => call(&root, &node, &script, params["arguments"]["serverId"].as_str().unwrap_or(""), "execute", params["arguments"]["command"].as_str().unwrap_or(""), params["arguments"]["timeoutSeconds"].as_u64().unwrap_or(60), true),
                    _ => Err("Unknown tool".into()),
                };
                Ok(match value { Ok(value) => json!({"content":[{"type":"text","text":value.to_string()}]}), Err(error) => json!({"isError":true,"content":[{"type":"text","text":error}]}) })
            },
            _ => Err("Unknown method".into()),
        };
        let response = match result { Ok(value) => json!({"jsonrpc":"2.0","id":id,"result":value}), Err(error) => json!({"jsonrpc":"2.0","id":id,"error":{"code":-32601,"message":error}}) };
        println!("{}", response); let _ = std::io::stdout().flush();
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn agent_cannot_connect_to_an_unselected_host() {
        let root = std::env::temp_dir().join(uuid::Uuid::new_v4().to_string());
        persist(&root, &[Host { id:"test".into(),name:"test".into(),host:"127.0.0.1".into(),port:22,username:"test".into(),auth:"password".into(),selected:false,fingerprint:None,host_key:None }]).unwrap();
        assert!(call(&root, Path::new("missing"), Path::new("missing"), "test", "execute", "whoami", 5, true).unwrap_err().contains("未被用户选择"));
        assert!(select(&root,"test",true).is_err());
        fs::remove_dir_all(root).unwrap();
    }
    #[cfg(windows)]
    #[test]
    fn large_credentials_roundtrip_in_system_store() {
        let id = uuid::Uuid::new_v4().to_string();
        let value = Secret { password: "测试密码".repeat(400), private_key: "private-key-test-data".repeat(400), passphrase:"test".into() };
        save_secret(&id, &value).unwrap();
        let loaded = secret(&id).unwrap();
        let item = secret_entry(&id).unwrap();
        delete_parts(&id, &item.get_password().unwrap()); let _ = item.delete_credential();
        assert_eq!(loaded.password, value.password); assert_eq!(loaded.private_key, value.private_key);
    }
}
