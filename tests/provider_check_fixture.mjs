// Offline companion fixture for the Pi and FrostPi approval flows.
import fs from 'node:fs';
import http from 'node:http';
import { compileRoutes, createBroker } from '../pi_sandbox_factory/containers/inference/broker.mjs';

const [socket, configFile, outcomeFile] = process.argv.slice(2);
const key = 'FACTORY_PRIVATE_PROVIDER_TEST_KEY';
const upstream = http.createServer((req, res) => {
  const authorized = req.method === 'GET' && req.url === '/v1/key' &&
    req.headers.authorization === 'Bearer ' + key;
  console.log(authorized ? 'PROVIDER CHECK SENT' : 'UNEXPECTED CHECK');
  res.writeHead(authorized ? Number(fs.readFileSync(outcomeFile, 'utf8')) : 401,
    { 'x-private-fixture': key }).end(key);
});
await new Promise(resolve => upstream.listen(0, '127.0.0.1', resolve));
const origin = 'http://127.0.0.1:' + upstream.address().port;
console.log(origin);
const config = await new Promise(resolve => {
  const timer = setInterval(() => {
    try {
      const data = JSON.parse(fs.readFileSync(configFile, 'utf8'));
      clearInterval(timer);
      resolve(data);
    } catch {}
  }, 50);
});
const broker = createBroker(compileRoutes(config, { allowHttpFixture: true }), () => ({ origin, key }));
broker.listen(socket);
