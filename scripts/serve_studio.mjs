/** Serve built Studio assets and stream /api (including Range and WebSocket).
 * No Vite transforms on the user-facing port. No provider credentials in web config. */
import { createServer, request as httpRequest } from 'node:http'
import { request as httpsRequest } from 'node:https'
import { accessSync, constants, createReadStream, readFileSync, realpathSync, statSync } from 'node:fs'
import { readFile, stat } from 'node:fs/promises'
import { resolve, extname, sep } from 'node:path'
import { fileURLToPath } from 'node:url'
import { parseArgs, promisify } from 'node:util'
import { pipeline } from 'node:stream'
import { createGzip, gzip } from 'node:zlib'

const compress = promisify(gzip)
const types = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8', '.json': 'application/json', '.svg': 'image/svg+xml', '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.webp': 'image/webp', '.ico': 'image/x-icon', '.woff': 'font/woff', '.woff2': 'font/woff2', '.mp4': 'video/mp4' }
function acceptsGzip(value) {
  const codings = new Map(String(value ?? '').split(',').map((entry) => {
    const [name, ...parameters] = entry.trim().toLowerCase().split(';')
    const quality = parameters.map((part) => part.trim()).find((part) => part.startsWith('q='))
    return [name, quality === undefined ? 1 : Number(quality.slice(2))]
  }))
  return (codings.get('gzip') ?? codings.get('*') ?? 0) > 0
}
function jsonResponseHeaders(req, reply) {
  const headers = { ...reply.headers }
  const compressible = req.method !== 'HEAD' && !req.headers.range && !headers['content-range']
    && ![204, 206, 304].includes(reply.statusCode) && !headers['content-encoding']
    && /^application\/(?:[\w.-]+\+)?json(?:\s*;|$)/i.test(headers['content-type'] ?? '')
    && !/\bno-transform\b/i.test(`${req.headers['cache-control'] ?? ''},${headers['cache-control'] ?? ''}`)
    && (!headers['content-length'] || Number(headers['content-length']) >= 1024)
  if (!compressible) return { headers, gzip: false }
  const vary = String(headers.vary ?? '').split(',').map((part) => part.trim()).filter(Boolean)
  if (!vary.some((name) => ['*', 'accept-encoding'].includes(name.toLowerCase()))) vary.push('Accept-Encoding')
  headers.vary = vary.join(', ')
  if (!acceptsGzip(req.headers['accept-encoding'])) return { headers, gzip: false }
  delete headers['content-length']
  headers['content-encoding'] = 'gzip'
  // A transformed representation must not retain the origin's strong byte validator.
  if (headers.etag && !headers.etag.startsWith('W/')) headers.etag = `W/${headers.etag}`
  return { headers, gzip: true }
}
export function studioWebServer({ root, target }) {
  const directory = resolve(root), backend = new URL(target), cache = new Map()
  if (!['http:', 'https:'].includes(backend.protocol)) throw new Error('API target must be HTTP(S)')
  const request = backend.protocol === 'https:' ? httpsRequest : httpRequest
  const api = (url) => url === '/api' || url.startsWith('/api/')
  const fail = (res, status, message) => { if (res.headersSent) { res.destroy(); return } res.writeHead(status, { 'Content-Type': 'text/plain; charset=utf-8', 'Cache-Control': 'no-store' }); res.end(message) }
  const server = createServer(async (req, res) => {
    if (api(req.url ?? '')) {
      const upstream = request(new URL(req.url, backend), { method: req.method, headers: { ...req.headers, host: backend.host } }, (reply) => {
        const response = jsonResponseHeaders(req, reply)
        res.writeHead(reply.statusCode ?? 502, response.headers)
        if (response.gzip) pipeline(reply, createGzip(), res, () => {})
        else { reply.on('error', () => res.destroy()); reply.pipe(res) }
      })
      upstream.setTimeout(120_000, () => upstream.destroy(new Error('API read timeout')))
      upstream.on('error', () => fail(res, 502, '服务暂时无法连接，请重试。'))
      req.on('aborted', () => upstream.destroy())
      res.on('close', () => { if (!res.writableFinished) upstream.destroy() })
      req.pipe(upstream); return
    }
    if (!['GET', 'HEAD'].includes(req.method)) { fail(res, 405, 'Method not allowed'); return }
    try {
      const pathname = decodeURIComponent(new URL(req.url, 'http://studio.local').pathname)
      if (pathname.includes('\0') || pathname.split('/').includes('..')) { fail(res, 400, 'Invalid path'); return }
      let path = resolve(directory, '.' + pathname)
      if (!path.startsWith(directory + sep) && path !== directory) { fail(res, 404, 'Not found'); return }
      if (path === directory || !extname(pathname)) path = resolve(directory, 'index.html')
      const info = await stat(path)
      if (!info.isFile()) { fail(res, 404, 'Not found'); return }
      const etag = `"${info.size}-${info.mtimeMs}"`
      const headers = { 'Content-Type': types[extname(path)] ?? 'application/octet-stream', ETag: etag,
        'Cache-Control': /\/assets\/[^/]+-[\w-]{8,}\.[\w.]+$/.test(pathname) ? 'public, max-age=31536000, immutable' : 'no-cache',
        'X-Content-Type-Options': 'nosniff', 'Accept-Ranges': 'bytes' }
      if (req.headers['if-none-match'] === etag) { res.writeHead(304, headers); res.end(); return }
      if (req.headers.range) {
        const match = /^bytes=(\d*)-(\d*)$/.exec(req.headers.range)
        const start = match?.[1] ? Number(match[1]) : Math.max(0, info.size - Number(match?.[2]))
        const end = match?.[1] && match[2] ? Math.min(info.size - 1, Number(match[2])) : info.size - 1
        if (!match || (!match[1] && !match[2]) || start > end || start >= info.size) { res.writeHead(416, { ...headers, 'Content-Range': `bytes */${info.size}` }); res.end(); return }
        res.writeHead(206, { ...headers, 'Content-Range': `bytes ${start}-${end}/${info.size}`, 'Content-Length': end - start + 1 })
        if (req.method === 'HEAD') res.end()
        else { const stream = createReadStream(path, { start, end }); stream.on('error', () => res.destroy()); res.on('close', () => stream.destroy()); stream.pipe(res) }
        return
      }
      const encoded = acceptsGzip(req.headers['accept-encoding']) && /\.(js|css|html|json|svg)$/.test(path) && info.size > 1024
      if (info.size <= 4 * 1024 * 1024) {
        let saved = cache.get(path)
        if (!saved || saved.etag !== etag) { saved = { etag, body: await readFile(path) }; cache.set(path, saved); if (cache.size > 256) cache.delete(cache.keys().next().value) }
        if (encoded && !saved.gzip) saved.gzip = await compress(saved.body)
        const body = encoded ? saved.gzip : saved.body
        res.writeHead(200, { ...headers, Vary: 'Accept-Encoding', ...(encoded ? { 'Content-Encoding': 'gzip' } : {}), 'Content-Length': body.length })
        res.end(req.method === 'HEAD' ? undefined : body)
      } else {
        res.writeHead(200, { ...headers, 'Content-Length': info.size })
        if (req.method === 'HEAD') res.end()
        else { const stream = createReadStream(path); stream.on('error', () => res.destroy()); res.on('close', () => stream.destroy()); stream.pipe(res) }
      }
    } catch (error) { fail(res, error?.code === 'ENOENT' || error?.code === 'ENOTDIR' ? 404 : 503, '页面文件暂时无法读取。') }
  })
  server.on('upgrade', (req, socket, head) => {
    if (!api(req.url ?? '')) { socket.end('HTTP/1.1 404 Not Found\r\n\r\n'); return }
    const upstream = request(new URL(req.url, backend), { headers: { ...req.headers, host: backend.host } })
    upstream.on('upgrade', (reply, peer, peerHead) => {
      socket.write(`HTTP/1.1 ${reply.statusCode} ${reply.statusMessage}\r\n${reply.rawHeaders.reduce((lines, value, index) => index % 2 ? lines : lines + value + ': ' + reply.rawHeaders[index + 1] + '\r\n', '')}\r\n`)
      if (head.length) peer.write(head)
      if (peerHead.length) socket.write(peerHead)
      peer.on('error', () => socket.destroy()); socket.on('error', () => peer.destroy())
      peer.on('close', () => socket.destroy()); socket.on('close', () => peer.destroy())
      socket.pipe(peer).pipe(socket)
    })
    upstream.on('response', (reply) => { socket.end(`HTTP/1.1 ${reply.statusCode} ${reply.statusMessage}\r\nConnection: close\r\nContent-Length: 0\r\n\r\n`); reply.resume() })
    upstream.on('error', () => socket.destroy())
    socket.on('error', () => upstream.destroy())
    upstream.end()
  })
  return server
}

function calledDirectly() {
  if (!process.argv[1]) return false
  try {
    return realpathSync(process.argv[1]) === realpathSync(fileURLToPath(import.meta.url))
  } catch {
    return false
  }
}

function requireFrontendFile(path) {
  try {
    const info = statSync(path)
    if (!info.isFile() || !info.size) throw new Error('Expected a non-empty regular file')
    accessSync(path, constants.R_OK)
  } catch {
    throw new Error(`Expected a readable, non-empty frontend file at ${path}.`)
  }
}

function requireFrontendBuild(directory) {
  const index = resolve(directory, 'index.html')
  requireFrontendFile(index)
  const html = readFileSync(index, 'utf8').replace(/<!--[\s\S]*?-->/g, '')
  // Vite puts its entry script and initial styles/preloads in index.html.
  const bootstrapTags = /<(?:script\b[^>]*\ssrc|link\b(?=[^>]*\srel\s*=\s*["'](?:stylesheet|modulepreload)["'])[^>]*\shref)\s*=\s*(["'])(?<reference>.*?)\1/gi
  const local = 'http://studio.local'
  for (const { groups: { reference } } of html.matchAll(bootstrapTags)) {
    const url = new URL(reference, local)
    if (url.origin !== local) continue
    const path = resolve(directory, '.' + decodeURIComponent(url.pathname))
    if (!path.startsWith(directory + sep)) throw new Error(`Invalid frontend asset in ${index}.`)
    requireFrontendFile(path)
  }
}

function runStudioCli() {
  const { values } = parseArgs({ options: {
    host: { type: 'string', default: '127.0.0.1' }, port: { type: 'string', default: '5175' },
    root: { type: 'string', default: fileURLToPath(new URL('../apps/web/dist', import.meta.url)) },
    'api-target': { type: 'string', default: process.env.VITE_API_PROXY_TARGET || 'http://127.0.0.1:8186' },
  } })
  try {
    requireFrontendBuild(resolve(values.root))
  } catch (error) {
    console.error(`Cannot start Studio. ${error.message} Run UV_NO_DEV=1 pnpm --dir apps/web build or use --root to select a built directory.`)
    process.exitCode = 1
    return
  }
  const server = studioWebServer({ root: values.root, target: values['api-target'] })
  server.listen(Number(values.port), values.host, () => process.stdout.write(`Studio built frontend listening on ${values.host}:${server.address().port}\n`))
  const sockets = new Set()
  server.on('connection', (socket) => { sockets.add(socket); socket.on('close', () => sockets.delete(socket)) })
  const stop = () => { server.close(); for (const socket of sockets) socket.destroy() }
  process.once('SIGTERM', stop); process.once('SIGINT', stop)
}

if (calledDirectly()) runStudioCli()
