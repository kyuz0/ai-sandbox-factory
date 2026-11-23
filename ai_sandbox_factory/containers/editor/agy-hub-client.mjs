// Forward one local native-extension endpoint to the filtered controller socket.
import net from 'node:net';

const connections = new Set();
const server = net.createServer(client => {
  const upstream = net.createConnection('/run/primary-editor/antigravity.sock');
  connections.add(client);
  connections.add(upstream);
  const finish = () => {
    client.destroy();
    upstream.destroy();
    connections.delete(client);
    connections.delete(upstream);
  };
  client.on('error', finish);
  upstream.on('error', finish);
  client.on('close', finish);
  upstream.on('close', finish);
  client.pipe(upstream).pipe(client);
});
server.listen(0, '127.0.0.1', () => process.stdout.write(String(server.address().port) + '\n'));
const stop = () => {
  server.close();
  for (const connection of connections) connection.destroy();
};
process.stdin.resume();
process.stdin.on('end', stop);
process.on('SIGTERM', stop);
process.on('SIGINT', stop);
