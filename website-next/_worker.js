const signupPath = '/updates/subscribe';
const maxBodyBytes = 2048;
const emailPattern = /^[^\s@,;<>]+@[a-z0-9-]+(?:\.[a-z0-9-]+)+$/i;

function reply(status, headers = {}) {
  return new Response(null, {
    status,
    headers: { 'Cache-Control': 'no-store', ...headers },
  });
}

async function readSignup(request) {
  if (!request.body) return null;

  const reader = request.body.getReader();
  const decoder = new TextDecoder();
  let size = 0;
  let body = '';
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > maxBodyBytes) {
        await reader.cancel();
        return null;
      }
      body += decoder.decode(value, { stream: true });
    }
    body += decoder.decode();
  } catch {
    return null;
  } finally {
    reader.releaseLock();
  }

  const addresses = new URLSearchParams(body).getAll('email');
  if (addresses.length !== 1) return null;
  const email = addresses[0].trim();
  return email.length <= 254 && emailPattern.test(email) ? email : null;
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname !== signupPath) return env.ASSETS.fetch(request);
    if (request.method !== 'POST') return reply(405, { Allow: 'POST' });
    if (request.headers.get('Origin') !== url.origin) return reply(403);

    const contentType = request.headers.get('Content-Type')?.split(';')[0].trim();
    if (contentType !== 'application/x-www-form-urlencoded') return reply(415);

    const email = await readSignup(request);
    if (!email) return reply(400);
    if (!env.SUBSCRIBERS) return reply(503);

    try {
      const result = await env.SUBSCRIBERS.prepare(
        'INSERT INTO subscribers (email) VALUES (?) ON CONFLICT(email) DO NOTHING',
      ).bind(email).run();
      return reply(result.success ? 204 : 503);
    } catch {
      return reply(503);
    }
  },
};
