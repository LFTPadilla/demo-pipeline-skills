import { createServer } from 'node:http';
import { readFileSync } from 'node:fs';

const port = Number(process.env.PORT ?? 47331);
const page = readFileSync(new URL('./index.html', import.meta.url));

createServer((req, res) => {
  if (req.url.startsWith('/api/')) {
    res.writeHead(200, { 'content-type': 'application/json' });
    res.end('[]');
    return;
  }
  res.writeHead(200, { 'content-type': 'text/html; charset=utf-8' });
  res.end(page);
}).listen(port);
