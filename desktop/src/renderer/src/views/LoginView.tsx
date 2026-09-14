import { useState } from 'react';
import { LockKeyhole, Loader2 } from 'lucide-react';
import { useApp } from '../store';
import { useT } from '../i18n';

export default function LoginView() {
  const { t } = useT();
  const { settings, platformLogin, updateSettings } = useApp();
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const submit = async () => {
    if (!username.trim() || !password) return;
    setBusy(true); setError('');
    try { await platformLogin(username, password); }
    catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    finally { setBusy(false); }
  };
  return (
    <div className="h-full w-full flex items-center justify-center drag" style={{ background: 'var(--bg)' }}>
      <div className="card no-drag w-[380px] p-7 flex flex-col gap-4" style={{ boxShadow: 'var(--shadow-sheet)' }}>
        <div className="w-11 h-11 rounded-xl flex items-center justify-center" style={{ background: 'var(--accent-soft)', color: 'var(--accent)' }}><LockKeyhole size={22} /></div>
        <div><div className="t-title">{t('登录 BioDSH 平台')}</div><div className="t-caption mt-1">{settings?.platformUrl}</div></div>
        <label className="flex flex-col gap-1"><span className="t-caption">{t('用户名')}</span><input className="field" autoFocus autoComplete="username" value={username} onChange={(e) => setUsername(e.target.value)} /></label>
        <label className="flex flex-col gap-1"><span className="t-caption">{t('密码')}</span><input className="field" type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} onKeyDown={(e) => e.key === 'Enter' && void submit()} /></label>
        {error && <div className="t-caption" style={{ color: 'var(--red)' }}>{error}</div>}
        <button className="btn btn-primary justify-center" disabled={busy || !username.trim() || !password} onClick={() => void submit()}>{busy && <Loader2 size={14} className="spin" />}{t('登录')}</button>
        <button className="btn btn-ghost justify-center" onClick={() => void updateSettings({ platformUrl: '' })}>{t('返回本地模式')}</button>
        <div className="t-caption text-center">{t('登录令牌由系统安全存储保护，不写入 settings.json')}</div>
      </div>
    </div>
  );
}
