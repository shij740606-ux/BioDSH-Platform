import { useEffect, useState } from 'react';
import { useApp } from '../store';
import type { SchedulingNode } from '@shared/types';
export default function ProjectCollaboration() {
 const {platform,settings}=useApp(); const id=settings?.currentProjectId || '';
 const [nodes,setNodes]=useState<SchedulingNode[]>([]);const [selected,setSelected]=useState<string[]>([]);
 const [chat,setChat]=useState('');const [busy,setBusy]=useState(false);const [notice,setNotice]=useState('');
 useEffect(() => {if (!id || !platform.user?.isAdmin) return;let alive=true;
  void Promise.all([window.biodsh.platformSchedulingNodes(),window.biodsh.platformProjectCollaboration(id,'nodes-get'),window.biodsh.platformProjectCollaboration(id,'chat-get')]).then(([list,ids,binding]) => {if(alive){setNodes(list);setSelected(ids as string[]);setChat((binding as {chat_id:string|null}).chat_id || '');setNotice('');}}).catch(reason => alive && setNotice(String(reason)));
  return () => {alive=false;};
 },[id,platform.user?.isAdmin]);
 if(!platform.user?.isAdmin || !id) return null;
 const save=async(action:string,body:unknown)=>{setBusy(true);setNotice('');try{await window.biodsh.platformProjectCollaboration(id,action,body);setNotice('已保存');}catch(reason){setNotice(String(reason));}finally{setBusy(false);}};
 return <div className="card p-4 flex flex-col gap-3"><div className="t-headline">管理员：当前项目服务器与飞书协作</div><div className="t-caption">项目：{platform.projects.find(p=>p.id===id)?.name}</div>
 <div className="flex flex-wrap gap-3">{nodes.map(n=><label key={n.id}><input type="checkbox" disabled={busy} checked={selected.includes(n.id)} onChange={event=>setSelected(previous=>event.target.checked?[...previous,n.id]:previous.filter(id=>id!==n.id))} /> {n.name}</label>)}</div>
 <button className="btn btn-ghost self-start" disabled={busy} onClick={()=>void save('nodes-set',selected)}>保存项目可用服务器</button>
 <label className="t-caption">飞书项目群 ID<input className="field w-full mt-1" placeholder="oc_…（留空解除绑定）" value={chat} onChange={event=>setChat(event.target.value)} /></label><div className="t-caption">管理员需配置飞书应用并将机器人加入项目群；绑定时平台验证群是否可访问。</div>
 <button className="btn btn-ghost self-start" disabled={busy} onClick={()=>void save('chat-set',{chat_id:chat.trim() || null})}>保存项目群绑定</button>{notice && <div role="status" className="t-caption break-words">{notice}</div>}</div>;
}
