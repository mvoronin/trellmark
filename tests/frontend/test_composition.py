def test_public_factories_are_inert_and_disposal_rejects_pending_load(static_page):
    result = static_page.evaluate(
        """async () => {
          const effects = [], originals = [];
          for (const [owner, key] of [[Document.prototype, 'querySelector'],
            [Document.prototype, 'querySelectorAll'], [EventTarget.prototype, 'addEventListener'],
            [Storage.prototype, 'getItem'], [Storage.prototype, 'setItem'], [window, 'fetch']]) {
            originals.push([owner, key, owner[key]]);
            owner[key] = () => { effects.push(key); throw Error(key); };
          }
          let createShell, createBookmarks;
          try {
            ({createShell} = await import('/static/shell/index.js'));
            ({createBookmarks} = await import('/static/features/bookmarks/index.js'));
          } finally {
            for (const [owner, key, original] of originals) owner[key] = original;
          }
          const { createRequestLifetime } = await import('/static/shared/request.js');
          const html = await (await fetch('/index.html')).text();
          const parsed = new DOMParser().parseFromString(html, 'text/html');
          const root = document.createElement('div'); root.append(...parsed.body.children);
          document.body.append(root);
          let reads = 0, creates = 0, finish, unsubscribe = 0;
          const api = {
            setSessionExpiredHandler: () => () => { unsubscribe++; },
            getSession: async () => ({authenticated: true}),
            listGroups: () => { reads++; return new Promise(resolve => { finish = resolve; }); },
            createUrl: async () => { creates++; return {groups: []}; },
            siteIconPath: () => '',
          };
          const privateLifetime = createRequestLifetime();
          const shell = createShell(root, {api, privateLifetime,
            replaceGroups: groups => bookmarks.replaceGroups(groups), refreshGroups: () => bookmarks.ready(),
            onAuthenticated: () => bookmarks.authenticated(),
            onPrivateInvalidate: () => bookmarks.invalidate(), onPrivateClear: () => bookmarks.clear()});
          const bookmarks = createBookmarks(root, {api, privateLifetime,
            dialogs: shell.dialogs, status: shell.setStatus});
          const beforeStart = reads;
          await shell.start();
          shell.dispose(); bookmarks.dispose();
          root.querySelector('#url-input').value = 'https://example.test';
          root.querySelector('#url-form').dispatchEvent(new Event('submit', {cancelable: true}));
          finish({groups: [{id: 1, name: 'Private', parent_id: null, children: [], urls: [], domains: [], nsfw: false}]});
          await bookmarks.ready();
          return {effects, beforeStart, reads, creates, unsubscribe,
            count: root.querySelector('#url-count').textContent,
            groups: root.querySelector('#groups').children.length,
            status: root.querySelector('#form-status').textContent};
        }"""
    )
    assert result == {
        "effects": [],
        "beforeStart": 0,
        "reads": 1,
        "creates": 0,
        "unsubscribe": 1,
        "count": "0 saved",
        "groups": 0,
        "status": "",
    }
