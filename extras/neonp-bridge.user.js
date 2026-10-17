// ==UserScript==
// @name         Neo Now Playing - cover art bridge
// @namespace    neonp
// @version      1.0
// @description  Sends the cover URL from navigator.mediaSession to the Neo Now Playing plugin (127.0.0.1:47821)
// @match        https://music.youtube.com/*
// @match        https://www.youtube.com/*
// @match        https://open.spotify.com/*
// @grant        GM_xmlhttpRequest
// @connect      127.0.0.1
// @run-at       document-idle
// ==/UserScript==
(function () {
  'use strict';
  let lastKey = '', lastSent = 0;
  const area = (a) => { const m = /(\d+)x(\d+)/.exec(a.sizes || ''); return m ? (+m[1]) * (+m[2]) : 0; };
  setInterval(() => {
    const md = navigator.mediaSession && navigator.mediaSession.metadata;
    if (!md || !md.title || !md.artwork || !md.artwork.length) return;
    // largest available artwork (the plugin downsizes it anyway)
    const art = md.artwork.slice().sort((a, b) => area(b) - area(a))[0].src;
    const key = md.title + '|' + art;
    const now = Date.now();
    if (key === lastKey && now - lastSent < 20000) return;   // resend every 20 s (in case the plugin restarted)
    lastKey = key; lastSent = now;
    GM_xmlhttpRequest({
      method: 'POST',
      url: 'http://127.0.0.1:47821/art',
      headers: { 'Content-Type': 'application/json', 'X-NeoNP': '1' },
      data: JSON.stringify({ title: md.title, artist: md.artist || '', art: art }),
    });
  }, 2000);
})();
