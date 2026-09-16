import { useCallback, useEffect, useState } from 'react';
import { Copy, RefreshCw, Trash2 } from 'lucide-react';
import { useApp } from '../store';
import type { PlatformRole, SchedulingNode } from '@shared/types';

interface Member { user_id:string; username:string; display_name:string; avatar_url:string; role:PlatformRole }
interface Invitation { id:string; role:PlatformRole; expires_at:string; remaining_uses:number; revoked:boolean }
interface Notice { id:string; status:string; attempts:number; body:{kind?:string;status?:string} }

export default function ProjectCollaboration() {
  const { platform, settings } = useApp();
  const id = settings?.currentProjectId || '';
  const project = platform.projects.find((item) => item.id === id);
  const canManage = Boolean(platform.user?.isAdmin || project?.role === 'owner');
  const [nodes, setNodes] = useState<SchedulingNode[]>([]);
  const [selected, setSelected] = useState<string[]>([]);
  const [chat, setChat] = useState('');
  const [members, setMembers] = useState<Member[]>([]);
  const [invites, setInvites] = useState<Invitation[]>([]);
  const [notifications, setNotifications] = useState<Notice[]>([]);
  const [inviteRole, setInviteRole] = useState<PlatformRole>('analyst');
  const [inviteUrl, setInviteUrl] = useState('');
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState('');

  const call = useCallback((action:string, body:unknown = null) => window.biodsh.platformProjectCollaboration(id, action, body), [id]);
  const load = useCallback(async () => {
    if (!id || !canManage) return;
    const [nodeList, ids, binding, memberList, inviteList, noticeList] = await Promise.all([
      window.biodsh.platformSchedulingNodes(), call('nodes-get'), call('chat-get'),
      call('members-get'), call('invites-get'), call('notifications-get'),
    ]);
    setNodes(nodeList); setSelected(ids as string[]);
    setChat((binding as {chat_id:string|null}).chat_id || '');
    setMembers(memberList as Member[]); setInvites(inviteList as Invitation[]);
    setNotifications(noticeList as Notice[]);
  }, [call, canManage, id]);

  useEffect(() => { let active = true; void load().catch((reason) => active && setNotice(String(reason))); return () => { active = false; }; }, [load]);
  if (!canManage || !id) return null;

  const run = async (work:() => Promise<unknown>, success='已保存') => {
    setBusy(true); setNotice('');
    try { await work(); await load(); setNotice(success); }
    catch (reason) { setNotice(String(reason)); }
    finally { setBusy(false); }
  };
  const createInvite = () => run(async () => {
    const result = await call('invite-create', {role:inviteRole, days:7, uses:1}) as {url:string};
    setInviteUrl(result.url);
  }, '邀请链接已生成，有效期 7 天，可使用 1 次');

  return <div className="card p-5 flex flex-col gap-4">
    <div><div className="t-headline">项目协作</div><div className="t-caption">{project?.name} · 管理服务器范围、成员、邀请和飞书通知</div></div>
    <div className="flex flex-col gap-2"><div className="t-body font-semibold">项目可用服务器</div>
      <div className="flex flex-wrap gap-3">{nodes.map((node) => <label key={node.id}><input type="checkbox" disabled={busy} checked={selected.includes(node.id)} onChange={(event) => setSelected((previous) => event.target.checked ? [...previous, node.id] : previous.filter((value) => value !== node.id))} /> {node.name}</label>)}</div>
      <button className="btn btn-ghost self-start" disabled={busy} onClick={() => void run(() => call('nodes-set', selected))}>保存项目可用服务器</button>
    </div>
    <div className="hairline-t pt-4 flex flex-col gap-2"><div className="t-body font-semibold">项目成员</div>
      {members.map((member) => <div key={member.user_id} className="flex items-center gap-2 rounded-lg p-2" style={{background:'var(--surface-2)'}}>
        {member.avatar_url ? <img className="w-8 h-8 rounded-full" src={member.avatar_url} alt="" referrerPolicy="no-referrer" /> : <div className="w-8 h-8 rounded-full" style={{background:'var(--fill-2)'}} />}
        <div className="min-w-0 flex-1"><div className="t-body truncate">{member.display_name || member.username}</div><div className="t-caption truncate">{member.username}</div></div>
        <select className="field !w-[110px]" value={member.role} disabled={busy} onChange={(event) => void run(() => call('member-set', {user_id:member.user_id, role:event.target.value}))}>
          <option value="owner">负责人</option><option value="analyst">分析员</option><option value="viewer">只读</option>
        </select>
        <button className="btn btn-ghost" disabled={busy || member.user_id === platform.user?.id} title="移除成员" onClick={() => void run(() => call('member-remove', {user_id:member.user_id}), '成员已移除')}><Trash2 size={14}/></button>
      </div>)}
      <div className="flex gap-2"><select className="field !w-[130px]" value={inviteRole} onChange={(event) => setInviteRole(event.target.value as PlatformRole)}><option value="owner">负责人</option><option value="analyst">分析员</option><option value="viewer">只读</option></select><button className="btn btn-primary" disabled={busy} onClick={() => void createInvite()}>生成邀请链接</button></div>
      {inviteUrl && <div className="flex gap-2"><input className="field t-mono flex-1" readOnly value={inviteUrl}/><button className="btn btn-ghost" onClick={() => void navigator.clipboard.writeText(inviteUrl)}><Copy size={14}/>复制</button></div>}
      {invites.filter((invite) => !invite.revoked && invite.remaining_uses > 0).map((invite) => <div key={invite.id} className="t-caption flex items-center gap-2"><span>{invite.role} · 剩余 {invite.remaining_uses} 次 · {new Date(invite.expires_at).toLocaleString()}</span><button className="btn btn-ghost" disabled={busy} onClick={() => void run(() => call('invite-revoke', {id:invite.id}), '邀请已撤销')}>撤销</button></div>)}
    </div>
    <div className="hairline-t pt-4 flex flex-col gap-2"><div className="t-body font-semibold">飞书项目群</div>
      <input className="field" placeholder="oc_…（留空解除绑定）" value={chat} onChange={(event) => setChat(event.target.value)} />
      <div className="t-caption">将应用机器人加入项目群后填写群 ID。任务状态会以消息卡片发送；发送失败会在下面显示重试次数。</div>
      <button className="btn btn-ghost self-start" disabled={busy} onClick={() => void run(() => call('chat-set', {chat_id:chat.trim() || null}))}>保存项目群绑定</button>
      <div className="flex items-center gap-2"><span className="t-caption flex-1">最近通知</span><button className="btn btn-ghost" disabled={busy} onClick={() => void run(load, '已刷新')}><RefreshCw size={13}/>刷新</button></div>
      {notifications.length === 0 ? <div className="t-caption">暂无任务通知</div> : notifications.slice(0, 5).map((item) => <div key={item.id} className="t-caption">{item.body.kind || '任务'} · {item.body.status || '-'} · {item.status}{item.attempts > 1 ? ` · 已尝试 ${item.attempts} 次` : ''}</div>)}
    </div>
    {notice && <div role="status" className="t-caption break-words">{notice}</div>}
  </div>;
}
