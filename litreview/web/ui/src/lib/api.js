// JSON GETs against the litreview API.
export async function get(path, params = {}) {
  const query = new URLSearchParams(
    Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== "" && v !== false),
  ).toString();
  const res = await fetch(path + (query ? `?${query}` : ""));
  if (!res.ok) {
    let message = `${res.status} ${res.statusText}`;
    try {
      message = (await res.json()).detail || message;
    } catch {}
    throw new Error(message);
  }
  return res.json();
}

// JSON POSTs: the server only accepts changes as JSON from this origin.
export async function post(path, body = {}) {
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    let message = `${res.status} ${res.statusText}`;
    try {
      const detail = (await res.json()).detail;
      message = Array.isArray(detail) ? detail.map((d) => `${d.loc?.at(-1) ?? ""}: ${d.msg}`).join("; ") : detail || message;
    } catch {}
    throw new Error(message);
  }
  return res.json();
}

// A PDF sent as itself, the one change the server takes that isn't JSON.
export async function upload(path, file, params = {}) {
  const query = new URLSearchParams(Object.entries(params).filter(([, v]) => v)).toString();
  const res = await fetch(path + (query ? `?${query}` : ""), {
    method: "POST",
    headers: { "Content-Type": "application/pdf" },
    body: file,
  });
  if (!res.ok) {
    let message = `${res.status} ${res.statusText}`;
    try {
      message = (await res.json()).detail || message;
    } catch {}
    throw new Error(message);
  }
  return res.json();
}
