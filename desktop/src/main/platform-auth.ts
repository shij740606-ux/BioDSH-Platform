import { existsSync, readFileSync, unlinkSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { safeStorage } from 'electron';
import type { AppPaths, PlatformNode, PlatformNodeControlStatus, PlatformNodeMetric, PlatformNodeStatus, PlatformProject, PlatformSession, PlatformUser } from '../shared/types';

interface StoredSession { baseUrl: string; apiPath: string; accessToken: string; refreshToken: string }
interface TokenResponse { access_token: string; refresh_token: string; user: Record<string, unknown> }

function metricFromApi(raw: Record<string, unknown>): PlatformNodeMetric {
  return {
    id: Number(raw.id), nodeId: String(raw.node_id), cpuPercent: Number(raw.cpu_percent),
    loadOne: raw.load_one == null ? undefined : Number(raw.load_one),
    memoryUsedBytes: Number(raw.memory_used_bytes), memoryTotalBytes: Number(raw.memory_total_bytes), memoryPercent: Number(raw.memory_percent),
    diskUsedBytes: Number(raw.disk_used_bytes), diskTotalBytes: Number(raw.disk_total_bytes), diskPercent: Number(raw.disk_percent),
    networkRxBytes: Number(raw.network_rx_bytes), networkTxBytes: Number(raw.network_tx_bytes), runningJobs: Number(raw.running_jobs),
    gpus: Array.isArray(raw.gpus) ? raw.gpus as PlatformNodeMetric['gpus'] : [], collectedAt: String(raw.collected_at),
  };
}

function nodeFromApi(raw: Record<string, unknown>): PlatformNode {
  return {
    id: String(raw.id), name: String(raw.name), status: raw.status as PlatformNodeStatus,
    hostname: String(raw.hostname ?? ''), operatingSystem: String(raw.operating_system ?? ''), agentVersion: String(raw.agent_version ?? ''),
    capabilities: raw.capabilities as Record<string, unknown> ?? {},
    latestMetric: raw.latest_metric ? metricFromApi(raw.latest_metric as Record<string, unknown>) : undefined,
    lastHeartbeatAt: raw.last_heartbeat_at ? String(raw.last_heartbeat_at) : undefined,
  };
}

export class PlatformAuth {
  constructor(private readonly paths: AppPaths, private readonly getBaseUrl: () => string) {}

  private get file() { return path.join(this.paths.root, 'platform-session.bin'); }

  private load(): StoredSession | undefined {
    if (!existsSync(this.file) || !safeStorage.isEncryptionAvailable()) return undefined;
    try { return JSON.parse(safeStorage.decryptString(Buffer.from(readFileSync(this.file, 'utf8'), 'base64'))) as StoredSession; }
    catch { return undefined; }
  }

  private save(value: StoredSession): void {
    if (!safeStorage.isEncryptionAvailable()) throw new Error('系统安全存储不可用');
    writeFileSync(this.file, safeStorage.encryptString(JSON.stringify(value)).toString('base64'), { mode: 0o600 });
  }

  private clear(): void { if (existsSync(this.file)) unlinkSync(this.file); }

  private async discover(): Promise<{ baseUrl: string; apiPath: string }> {
    const baseUrl = this.getBaseUrl().trim().replace(/\/$/, '');
    if (!baseUrl) throw new Error('请先填写平台地址');
    const response = await fetch(`${baseUrl}/.well-known/biodsh-platform`);
    if (!response.ok) throw new Error(`平台发现失败：HTTP ${response.status}`);
    const metadata = await response.json() as { api_versions?: { v1?: string } };
    const apiPath = metadata.api_versions?.v1;
    if (!apiPath?.startsWith('/')) throw new Error('平台没有提供兼容的 v1 API');
    return { baseUrl, apiPath };
  }

  async login(username: string, password: string): Promise<PlatformSession> {
    const endpoint = await this.discover();
    const response = await fetch(`${endpoint.baseUrl}${endpoint.apiPath}/auth/login`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ username, password }) });
    if (!response.ok) throw new Error(response.status === 401 ? '用户名或密码错误' : `登录失败：HTTP ${response.status}`);
    const tokens = await response.json() as TokenResponse;
    this.save({ ...endpoint, accessToken: tokens.access_token, refreshToken: tokens.refresh_token });
    return this.status();
  }

  private async refresh(session: StoredSession): Promise<StoredSession> {
    const response = await fetch(`${session.baseUrl}${session.apiPath}/auth/refresh`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ refresh_token: session.refreshToken }) });
    if (!response.ok) { this.clear(); throw new Error('登录已过期，请重新登录'); }
    const tokens = await response.json() as TokenResponse;
    const next = { ...session, accessToken: tokens.access_token, refreshToken: tokens.refresh_token };
    this.save(next);
    return next;
  }

  private async request(session: StoredSession, resource: string, init: RequestInit = {}, retry = true): Promise<Response> {
    const response = await fetch(`${session.baseUrl}${session.apiPath}${resource}`, {
      ...init,
      headers: { ...init.headers, Authorization: `Bearer ${session.accessToken}` },
    });
    if (response.status === 401 && retry) return this.request(await this.refresh(session), resource, init, false);
    return response;
  }

  private session(): StoredSession {
    const session = this.load();
    if (!session || session.baseUrl !== this.getBaseUrl().trim().replace(/\/$/, '')) throw new Error('请先登录平台');
    return session;
  }

  async status(): Promise<PlatformSession> {
    const session = this.load();
    const configured = this.getBaseUrl().trim().replace(/\/$/, '');
    if (!session) return { authenticated: false, projects: [] };
    if (session.baseUrl !== configured) {
      this.clear();
      return { authenticated: false, projects: [] };
    }
    try {
      const meResponse = await this.request(session, '/auth/me');
      if (!meResponse.ok) throw new Error(`平台连接失败：HTTP ${meResponse.status}`);
      const projectResponse = await this.request(this.load() ?? session, '/projects');
      if (!projectResponse.ok) throw new Error(`项目读取失败：HTTP ${projectResponse.status}`);
      const rawUser = await meResponse.json() as Record<string, unknown>;
      const user: PlatformUser = { id: String(rawUser.id), username: String(rawUser.username), isActive: Boolean(rawUser.is_active), isAdmin: Boolean(rawUser.is_admin) };
      const projects = (await projectResponse.json() as Array<Record<string, unknown>>).map((p): PlatformProject => ({ id: String(p.id), name: String(p.name), description: String(p.description ?? ''), role: p.role as PlatformProject['role'] }));
      return { authenticated: true, user, projects };
    } catch (error) {
      if (String(error).includes('登录已过期')) return { authenticated: false, projects: [] };
      throw error;
    }
  }

  async nodes(): Promise<PlatformNode[]> {
    const response = await this.request(this.session(), '/nodes');
    if (!response.ok) throw new Error(response.status === 403 ? '只有管理员可以查看服务器' : `服务器读取失败：HTTP ${response.status}`);
    return (await response.json() as Array<Record<string, unknown>>).map(nodeFromApi);
  }

  async nodeMetrics(nodeId: string, minutes = 60): Promise<PlatformNodeMetric[]> {
    const response = await this.request(this.session(), `/nodes/${encodeURIComponent(nodeId)}/metrics?minutes=${minutes}`);
    if (!response.ok) throw new Error(`资源历史读取失败：HTTP ${response.status}`);
    return (await response.json() as Array<Record<string, unknown>>).map(metricFromApi);
  }

  async updateNode(nodeId: string, status: PlatformNodeControlStatus): Promise<PlatformNode> {
    const response = await this.request(this.session(), `/nodes/${encodeURIComponent(nodeId)}`, {
      method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ status }),
    });
    if (!response.ok) throw new Error(`服务器状态更新失败：HTTP ${response.status}`);
    return nodeFromApi(await response.json() as Record<string, unknown>);
  }

  async logout(): Promise<PlatformSession> {
    const session = this.load();
    if (session) await fetch(`${session.baseUrl}${session.apiPath}/auth/logout`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ refresh_token: session.refreshToken }) }).catch(() => undefined);
    this.clear();
    return { authenticated: false, projects: [] };
  }
}
