const assert = require('node:assert/strict');
const { generateKeyPairSync } = require('node:crypto');
const { spawn } = require('node:child_process');
const { Server, utils } = require('ssh2');
const path = require('node:path');
const key = generateKeyPairSync('rsa', { modulusLength: 2048 }).privateKey.export({ type: 'pkcs1', format: 'pem' });
const server = new Server({ hostKeys: [key] }, connection => {
  connection.on('error', () => {});
  connection.on('authentication', context => {
    if (context.username !== 'test') return context.reject();
    if (context.method === 'password' && context.password === 'test') return context.accept();
    const parsed = utils.parseKey(key);
    if (context.method === 'publickey' && context.key.data.equals(parsed.getPublicSSH()) && (!context.signature || parsed.verify(context.blob, context.signature, context.hashAlgo))) return context.accept();
    context.reject();
  });
  connection.on('ready', () => connection.on('session', accept => accept().on('exec', (accept, reject, info) => {
    const stream = accept(); stream.write('hello'); stream.stderr.write('note'); stream.exit(0); stream.end();
  })));
});
function request(value) {
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, [path.join(__dirname, 'client.cjs')]);
    let output = ''; child.stdout.on('data', chunk => { output += chunk; });
    child.on('error', reject); child.on('close', () => { try { resolve(JSON.parse(output)); } catch (error) { reject(error); } });
    child.stdin.end(JSON.stringify({ host: '127.0.0.1', port: server.address().port, username: 'test', password: 'test', timeoutSeconds: 5, ...value }));
  });
}
server.listen(0, '127.0.0.1', async () => {
  try {
    const probe = await request({ operation: 'probe' });
    assert.match(probe.fingerprint, /^SHA256:/); assert.ok(probe.hostKey);
    assert.ok((await request({ operation: 'execute', command: 'hello', hostKey: 'wrong' })).error);
    assert.ok((await request({ operation: 'test', hostKey: probe.hostKey, password: 'wrong' })).error);
    const result = await request({ operation: 'execute', command: 'hello', hostKey: probe.hostKey });
    assert.equal(result.stdout, 'hello'); assert.equal(result.stderr, 'note'); assert.equal(result.exitCode, 0);
    const keyResult = await request({ operation: 'execute', command: 'hello', hostKey: probe.hostKey, password: undefined, privateKey: key });
    assert.equal(keyResult.exitCode, 0);
    console.log('SSH probe, host-key rejection, authentication rejection and execution passed');
  } catch (error) { console.error(error); process.exitCode = 1; }
  finally { server.close(); }
});
