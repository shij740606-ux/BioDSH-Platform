const { Client } = require('ssh2');
const { createHash } = require('node:crypto');
const { StringDecoder } = require('node:string_decoder');
let input = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', chunk => { input += chunk; if (input.length > 262144) process.exit(1); });
process.stdin.on('end', () => {
  let request;
  try { request = JSON.parse(input); } catch { return finish({ error: 'SSH 请求无效' }); }
  const connection = new Client();
  let fingerprint = '', hostKey = '', finished = false;
  const timer = setTimeout(() => done({ error: 'SSH 操作超时；远程命令是否停止需另行确认' }), Math.min(300, Math.max(5, request.timeoutSeconds || 30)) * 1000);
  function done(value) {
    if (finished) return;
    finished = true; clearTimeout(timer); connection.destroy(); finish(value);
  }
  connection.on('error', error => done({ error: request.operation === 'probe' && hostKey ? undefined : (error.level === 'client-authentication' ? 'SSH 认证失败，请检查用户名、密码或私钥' : error.message), fingerprint, hostKey }));
  connection.on('ready', () => {
    if (request.operation !== 'execute') return done({ fingerprint, hostKey, connected: true });
    connection.exec(request.command, (error, stream) => {
      if (error) return done({ error: error.message });
      let stdout = '', stderr = '', total = 0;
      const outDecoder = new StringDecoder('utf8'), errDecoder = new StringDecoder('utf8');
      function collect(chunk, target) {
        total += chunk.length;
        if (total > 1048576) return done({ error: '输出超过 1 MB，请将结果写入服务器文件后分段查看' });
        if (target === 'stdout') stdout += outDecoder.write(chunk); else stderr += errDecoder.write(chunk);
      }
      stream.on('data', chunk => collect(chunk, 'stdout'));
      stream.stderr.on('data', chunk => collect(chunk, 'stderr'));
      stream.on('error', error => done({ error: error.message }));
      stream.on('close', (code, signal) => done({ stdout: stdout + outDecoder.end(), stderr: stderr + errDecoder.end(), exitCode: code ?? null, signal: signal ?? null }));
    });
  });
  try {
    connection.connect({ host: request.host, port: request.port, username: request.username,
      password: request.operation === 'probe' ? undefined : request.password,
      privateKey: request.operation === 'probe' ? undefined : request.privateKey,
      passphrase: request.passphrase, readyTimeout: 15000,
      hostVerifier: key => {
        hostKey = key.toString('base64');
        fingerprint = 'SHA256:' + createHash('sha256').update(key).digest('base64').replace(/=+$/, '');
        if (request.operation === 'probe') { setImmediate(() => done({ fingerprint, hostKey })); return false; }
        return request.hostKey === hostKey;
      },
    });
  } catch (error) { done({ error: error.message }); }
});
function finish(value) { process.stdout.write(JSON.stringify(value)); }
