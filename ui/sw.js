// K-3f: service worker for Web Push only (docs/plan-k.md Ek B7). No caching:
// the page always comes from the gateway, so nothing stale can be shown.
self.addEventListener("push", (event) => {
  let data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch {
    data = { body: event.data ? event.data.text() : "" };
  }
  // iOS requires every push to show a notification.
  event.waitUntil(self.registration.showNotification(data.title || "SAVAS AI", {
    body: data.body || "",
    tag: data.tag || undefined,
    icon: "/ui/icons/icon-192.png",
    badge: "/ui/icons/icon-192.png",
    data: { url: data.url || "/ui/" },
  }));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const url = new URL(event.notification.data?.url || "/ui/", self.location.origin).href;
  event.waitUntil((async () => {
    const windows = await clients.matchAll({ type: "window", includeUncontrolled: true });
    for (const client of windows) {
      if (client.url.includes("/ui/")) {
        await client.focus();
        if ("navigate" in client) await client.navigate(url);
        return;
      }
    }
    await clients.openWindow(url);
  })());
});
