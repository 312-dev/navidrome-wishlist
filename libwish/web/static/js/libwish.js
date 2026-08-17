/* The live connection, the keyboard, and the audio preview.
 *
 * One rule governs everything below: an arriving track must never move content
 * under a reading user. A wishlist that reorders itself while you are deciding
 * whether to spend money on something is worse than one that updates on a
 * timer, and unexpected motion is the single worst thing this interface can do
 * to someone with ADHD or visual snow. Rows therefore arrive only when the
 * reader is demonstrably not reading, and every insertion that could push the
 * viewport re-anchors the scroll position afterwards.
 *
 * The server decides what a row looks like. Events carry facts, not markup, so
 * when one lands the client refetches the affected fragment. The one exception
 * is claim progress, which is pure presentation of fields already in the event
 * and would otherwise cost a round trip five times per purchase.
 *
 * The page holds a window over the list rather than the whole of it. Rows
 * arrive from the server in two ways and both obey the rule above: a window
 * asked for by the reader appends below everything on screen, and a live
 * arrival either inserts at the top when nobody is reading or waits behind a
 * counter until asked for.
 */
(function () {
  'use strict';

  var rack = null;
  var announcer = null;
  var audio = null;
  var playingId = null;
  var more = null;
  var bulk = null;

  /* Track ids that arrived while someone was reading. They wait here until the
     user asks for them. */
  var pending = [];
  var stages = {};
  var phaseOrder = [];

  function $(sel, root) { return (root || document).querySelector(sel); }
  function $$(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }

  function say(text) {
    if (announcer) announcer.textContent = text;
  }

  function reducedMotion() {
    return window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  }

  /* ------------------------------------------------------------------ *
   * fragments
   * ------------------------------------------------------------------ */

  function fragment(url) {
    return fetch(url, { headers: { 'X-Requested-With': 'libwish' }, credentials: 'same-origin' })
      .then(function (res) { return res.ok ? res.text() : null; })
      .catch(function () { return null; });
  }

  /* Swap in new markup and put the reader back where they were.
   *
   * Anything inserted above the viewport lengthens the page above the fold,
   * which scrolls the content under the cursor down by exactly that much. The
   * fix is to measure the growth and give it straight back. */
  function preservingScroll(mutate) {
    var before = document.documentElement.scrollHeight;
    var y = window.scrollY;
    mutate();
    var grew = document.documentElement.scrollHeight - before;
    if (grew !== 0 && y > 0) window.scrollTo({ top: y + grew, behavior: 'auto' });
  }

  /* ------------------------------------------------------------------ *
   * covers
   * ------------------------------------------------------------------ */

  /* Cover art is served by this app from a local cache and answers 404 until
     the cache holds it, which early on is most of the list. Hiding the image
     uncovers the mark the cell already draws underneath, so a miss looks like
     a designed empty sleeve and the cell keeps its size either way. */
  function coverFailed(img) {
    img.hidden = true;
  }

  /* An image can finish, or fail, before this file runs and before a fragment
     is inserted. `complete` with no intrinsic width is a load that failed, and
     it is the only way to catch the ones whose error event already went by. */
  function sweepCovers(root) {
    $$('img[data-cover]', root).forEach(function (img) {
      if (img.complete && img.naturalWidth === 0) coverFailed(img);
    });
  }

  /* Error does not bubble, so it is caught on the way down. */
  function watchCovers() {
    document.addEventListener('error', function (e) {
      var img = e.target;
      if (img && img.tagName === 'IMG' && img.hasAttribute('data-cover')) coverFailed(img);
    }, true);
    sweepCovers(document);
  }

  /* ------------------------------------------------------------------ *
   * windowing
   * ------------------------------------------------------------------ */

  function num(value, fallback) {
    var n = parseInt(value, 10);
    return isNaN(n) ? fallback : n;
  }

  function moreNote(text) {
    var note = $('#more-note');
    if (!note) return;
    note.textContent = text || '';
    note.hidden = !text;
  }

  /* Fold an arriving window into the rack. A row the page already holds is
     dropped by id, which is what keeps a window that overlaps the last one
     from showing anything twice. */
  function appendWindow(holder) {
    var added = 0;
    Array.prototype.slice.call(holder.children).forEach(function (node) {
      if (node.id && document.getElementById(node.id)) return;
      rack.appendChild(node);
      if (node.classList.contains('row')) added += 1;
    });
    return added;
  }

  function paintMore() {
    if (!more) return;
    var offset = num(more.dataset.offset, 0);
    var total = num(more.dataset.total, 0);
    var step = num(more.dataset.limit, 60);
    var left = Math.max(total - offset, 0);
    var shown = $('#more-shown');
    var label = $('#more-label');
    var link = $('#more-load');
    if (shown) shown.textContent = String(offset);
    if (label) label.textContent = 'Load ' + Math.min(step, left) + ' more';
    if (link) {
      link.href = '/?show=' + (offset + step);
    }
    more.hidden = left === 0;
  }

  /* Rows append below everything on screen, which is why this needs no scroll
     correction: nothing above the reader's position changes. */
  function loadMore(all) {
    if (!rack || !more || more.dataset.busy === 'true') return Promise.resolve();
    var offset = num(more.dataset.offset, 0);
    var total = num(more.dataset.total, 0);
    var left = Math.max(total - offset, 0);
    if (!left) { paintMore(); return Promise.resolve(); }
    var limit = all ? left : Math.min(num(more.dataset.limit, 60), left);
    more.dataset.busy = 'true';
    moreNote('');
    var url = '/ui/page?view=' + encodeURIComponent(rack.dataset.view || 'wanted') +
              '&offset=' + offset + '&limit=' + limit;
    return fragment(url).then(function (html) {
      more.dataset.busy = 'false';
      if (html === null) {
        moreNote('The next rows did not load. The list on screen is still current. Try again.');
        say('The next rows did not load.');
        return;
      }
      var holder = document.createElement('ul');
      holder.innerHTML = html;
      var added = appendWindow(holder);
      more.dataset.offset = String(offset + added);
      paintMore();
      sweepCovers(rack);
      syncPicks();
      if (window.htmx) window.htmx.process(rack);
      say(added + (added === 1 ? ' more track loaded, ' : ' more tracks loaded, ') +
          more.dataset.offset + ' of ' + total + ' on screen');
    });
  }

  function markNew(row) {
    row.classList.add('row--new');
    if (!reducedMotion()) row.classList.add('row--entering');
    var hold = parseInt(getComputedStyle(rack).getPropertyValue('--marker-hold'), 10) || 2000;
    window.setTimeout(function () { row.classList.add('row--settled'); }, hold);
  }

  /* ------------------------------------------------------------------ *
   * insertion
   * ------------------------------------------------------------------ */

  function readerIsBusy() {
    if (window.scrollY > 8) return true;
    if (document.activeElement && document.activeElement.closest &&
        document.activeElement.closest('#rack')) return true;
    if ($('.plate.is-working')) return true;
    return false;
  }

  /* A list with nothing in it renders an empty state instead of a rack, so
     the first row to arrive live has nowhere to go. That is not an edge case
     on the Owned tab: it is the first purchase anyone ever files, landing on
     a page that still says nothing has been claimed yet.

     The empty block carries the view the rack would have had, so it can be
     exchanged for one rather than having that string written a second time
     here. Returns null where there is no rack and nothing standing in for
     one, which is the first-run page. */
  /* Which list this page is, whether or not it has a rack yet. Read-only on
     purpose: deciding that an event does not belong here must not cost the
     empty state its message. */
  function currentView() {
    var el = document.getElementById('rack') || document.querySelector('.empty[data-view]');
    return (el && el.dataset.view) || 'wanted';
  }

  function ensureRack() {
    if (rack && document.body.contains(rack)) return rack;
    rack = document.getElementById('rack');
    if (rack) return rack;
    var empty = document.querySelector('.empty[data-view]');
    if (!empty) return null;
    var list = document.createElement('ul');
    list.className = 'rack';
    list.id = 'rack';
    list.setAttribute('role', 'list');
    list.dataset.view = empty.dataset.view;
    empty.parentNode.replaceChild(list, empty);
    rack = list;
    return rack;
  }

  function insertRows(ids, announce) {
    if (!rack || !ids.length) return Promise.resolve();
    var view = rack.dataset.view || 'wanted';
    var url = '/ui/rows?view=' + encodeURIComponent(view) +
              '&ids=' + ids.join(',');
    return fragment(url).then(function (html) {
      if (!html) return;
      var holder = document.createElement('ul');
      holder.innerHTML = html;
      var rows = $$('.row', holder);
      preservingScroll(function () {
        rows.forEach(function (row) {
          if (document.getElementById(row.id)) return;
          /* The top, because the list is newest first and an arrival is the
             newest thing in it. */
          rack.insertBefore(row, rack.firstChild);
          markNew(row);
        });
      });
      if (window.htmx) window.htmx.process(rack);
      sweepCovers(rack);
      /* A live arrival lengthens the list and lands inside the part of it
         already on screen, so both figures move together and "60 of 160"
         stays true. A row that a later window sends again is dropped by id,
         which is what keeps the two counts from drifting apart. */
      if (more && rows.length) {
        more.dataset.total = String(num(more.dataset.total, 0) + rows.length);
        more.dataset.offset = String(num(more.dataset.offset, 0) + rows.length);
        paintMore();
      }
      if (!rows.length) return;
      say(announce ? announce(rows.length)
                   : (rows.length === 1 ? 'One new love added to the list'
                                        : rows.length + ' new loves added to the list'));
    });
  }

  function showPill() {
    var pill = $('#newpill');
    if (!pill) return;
    $('#newpill-n').textContent = String(pending.length);
    pill.hidden = pending.length === 0;
  }

  function onTrackAdded(data) {
    if (!ensureRack() || rack.dataset.view !== 'wanted') return;
    if (document.getElementById('row-' + data.id)) return;
    if (readerIsBusy()) {
      if (pending.indexOf(data.id) === -1) pending.push(data.id);
      showPill();
      say(pending.length + ' new loves waiting');
      return;
    }
    insertRows([data.id]);
  }

  function onTrackRemoved(data) {
    var row = document.getElementById('row-' + data.id);
    if (!row) return;
    var drop = function () {
      preservingScroll(function () {
        row.remove();
      });
      /* A row that left the list cannot be part of a confirm, and the count
         in the bar has to say so before anyone presses anything. */
      if (picked[String(data.id)] !== undefined) {
        delete picked[String(data.id)];
        paintBulk();
      }
      if (more) {
        more.dataset.total = String(Math.max(num(more.dataset.total, 0) - 1, 0));
        more.dataset.offset = String(Math.max(num(more.dataset.offset, 0) - 1, 0));
        paintMore();
      }
    };
    if (reducedMotion()) { drop(); return; }
    row.classList.add('row--leaving');
    window.setTimeout(drop, 140);
  }

  /* Ignoring a track, restoring one and filing a purchase all arrive as an
     update carrying the new status rather than as a removal, so the view has
     to decide for itself whether the row belongs on screen. */
  var BELONGS = {
    wanted: ['queued'],
    owned: ['purchased', 'owned'],
    ignored: ['ignored']
  };

  /* What the reader is told when a status change brings a row onto the list
     they are looking at. Named per view because "added" is not what happened:
     the track was already on the list, it moved. */
  var ARRIVED = {
    wanted: function (n) {
      return n === 1 ? 'One track is back on the list' : n + ' tracks are back on the list';
    },
    owned: function (n) {
      return n === 1 ? 'One track landed in your library' : n + ' tracks landed in your library';
    },
    ignored: function (n) {
      return n === 1 ? 'One track moved to ignored' : n + ' tracks moved to ignored';
    }
  };

  function onTrackUpdated(d) {
    var view = currentView();
    var allowed = BELONGS[view] || [];
    var belongs = !!d.status && allowed.indexOf(d.status) !== -1;
    var row = document.getElementById('row-' + d.id);

    if (!row) {
      /* The other half of the move. A claim landing takes a row off Wanted,
         and until this existed it put nothing on Owned: the counts in the
         tabs went up while the list under them stayed as it was, so the
         purchase the reader had just watched complete was not there until
         they reloaded the page.

         An update carrying no status is not a move and must not conjure a
         row: those are enrichments arriving for a track this view never
         had. */
      if (belongs && ensureRack()) insertRows([d.id], ARRIVED[view]);
      return;
    }
    if (d.status && !belongs) {
      onTrackRemoved(d);
      return;
    }
    refreshRow(d.id);
  }

  /* ------------------------------------------------------------------ *
   * tab counts
   * ------------------------------------------------------------------ */

  var countsTimer = null;

  /* Asked for, never worked out from the event. A count kept by adding one and
     taking one away is right until the first event that does not arrive, and
     the stream is explicitly allowed to drop them from a client that has
     fallen behind. The server already knows the answer.

     Coalesced, because confirming forty claims at once produces forty events
     inside a second and the tabs only have to be right at the end of it. */
  function countsChanged() {
    if (countsTimer) return;
    countsTimer = window.setTimeout(function () {
      countsTimer = null;
      paintCounts();
    }, 250);
  }

  function paintCounts() {
    fetch('/api/counts', { headers: { 'X-Requested-With': 'libwish' }, credentials: 'same-origin' })
      .then(function (res) { return res.ok ? res.json() : null; })
      .then(function (counts) {
        if (!counts) return;
        $$('.tab__count').forEach(function (el) {
          var view = el.dataset.count;
          if (view && counts[view] !== undefined) el.textContent = String(counts[view]);
        });
      })
      .catch(function () { /* the numbers stay as they were, which is honest */ });
  }

  /* ------------------------------------------------------------------ *
   * plates and claims
   * ------------------------------------------------------------------ */

  function refreshRow(trackId) {
    var plate = document.getElementById('plate-' + trackId);
    var detail = document.getElementById('detail-' + trackId);
    if (plate) {
      fragment('/ui/plate/' + trackId).then(function (html) {
        if (!html) return;
        var was = plate.dataset.state;
        plate.outerHTML = html;
        var fresh = document.getElementById('plate-' + trackId);
        /* The press fires once, at the moment a purchase becomes a file. It is
           the only orchestrated motion in the application. */
        if (fresh && fresh.dataset.state === 'owned' && was !== 'owned' && !reducedMotion()) {
          fresh.classList.add('plate--pressing');
          window.setTimeout(function () {
            fresh.classList.remove('plate--pressing');
            fresh.style.willChange = '';
          }, 240);
        }
        /* A track that is claiming, or that already landed, drops out of the
           selection and stops offering itself: confirming it a second time
           would be paying twice for the same row. */
        var box = document.querySelector('.pick[data-pick="' + trackId + '"]');
        if (box && fresh && (fresh.dataset.state === 'working' || fresh.dataset.state === 'owned')) {
          if (box.checked) setPicked(box, false);
          box.disabled = true;
        }
      });
    }
    if (detail) {
      fragment('/ui/detail/' + trackId).then(function (html) {
        if (html !== null) {
          detail.innerHTML = html;
          if (window.htmx) window.htmx.process(detail);
        }
      });
    }
  }

  function formatMB(bytes) {
    return (bytes / 1048576).toFixed(1);
  }

  function onJobProgress(d) {
    if (d.kind !== 'claim' || !d.track_id) return;
    var plate = document.getElementById('plate-' + d.track_id);
    if (!plate || plate.dataset.state !== 'working') { refreshRow(d.track_id); return; }

    var stage = stages[d.phase] || {};
    var step = d.step || (phaseOrder.indexOf(d.phase) + 1);
    var of = d.of || phaseOrder.length;

    var lines = plate.querySelectorAll('.plate__line span:first-child');
    if (lines[0]) lines[0].textContent = stage.label || String(d.phase).toUpperCase();
    if (lines[1]) lines[1].textContent = step + ' OF ' + of;
    plate.setAttribute('aria-valuenow', String(step));
    plate.setAttribute('aria-valuetext', (stage.label || d.phase).toLowerCase() +
                       ', step ' + step + ' of ' + of);
    $$('.plate__step', plate).forEach(function (seg, i) {
      seg.classList.toggle('is-done', i < step);
    });

    var claim = document.querySelector('[data-claim="' + d.track_id + '"]');
    if (claim) {
      claim.dataset.phase = d.phase;
      var line = $('[data-claim-line]', claim);
      if (line && stage.line) {
        line.textContent = stage.line
          .replace('{store}', d.store || 'Qobuz')
          .replace('{title}', claim.dataset.title || '')
          .replace('{artist}', claim.dataset.artist || '');
      }
      var rule = $('.claim__rule', claim);
      var fill = $('[data-claim-fill]', claim);
      var determinate = d.bytes_total > 0;
      if (rule) rule.dataset.determinate = determinate ? 'true' : 'false';
      if (fill && determinate) {
        fill.style.width = Math.round((d.bytes_done / d.bytes_total) * 100) + '%';
      }
      var bytes = $('[data-claim-bytes]', claim);
      if (bytes && determinate) {
        bytes.textContent = formatMB(d.bytes_done) + ' of ' + formatMB(d.bytes_total) + ' MB';
      }
    }
    say((stage.label || d.phase) + ', step ' + step + ' of ' + of);
  }

  /* ------------------------------------------------------------------ *
   * selection and bulk confirm
   * ------------------------------------------------------------------ *
   *
   * Confirming a claim tells the software to go through with a purchase it was
   * not confident enough to make on its own, so confirming a hundred of them
   * at once is the most expensive thing this interface can do. Three rules
   * follow from that, and every branch below exists to hold one of them:
   *
   *   the number is stated in a sentence before anything is sent,
   *   the press that arms the confirm is never the press that fires it, and
   *   what has been sent can be stopped.
   *
   * The store matters too. One confirm carries one store, so a selection that
   * spans two of them sends the tracks at the chosen store and says plainly
   * how many it left behind rather than quietly claiming them somewhere they
   * were never listed.
   */

  var picked = Object.create(null); /* track id -> the store it is listed at */
  var stage = 'pick';
  var armedAt = 0;
  var sentIds = [];
  var lastPicked = -1;
  var shiftHeld = false;

  function pickCount() {
    return Object.keys(picked).length;
  }

  function pickBoxes() {
    return rack ? $$('.pick', rack) : [];
  }

  function storeLabel(id) {
    return id ? id.charAt(0).toUpperCase() + id.slice(1) : 'the store';
  }

  /* Selected rows that the named store actually sells. A row carries every
     store it can be bought at rather than one it has been assigned to: most
     rows are assigned to none, and treating that as "no store" is what left
     the selection column empty and this bar with nothing to offer. */
  function idsFor(store) {
    return Object.keys(picked).filter(function (id) {
      return picked[id].indexOf(store) !== -1;
    });
  }

  function setPicked(box, on, quiet) {
    box.checked = on;
    if (on) picked[box.dataset.pick] = (box.dataset.stores || '').split(',').filter(Boolean);
    else delete picked[box.dataset.pick];
    var row = box.closest('.row');
    if (row) row.classList.toggle('is-picked', on);
    if (!quiet) paintBulk();
  }

  function clearPicks(quiet) {
    pickBoxes().forEach(function (box) { setPicked(box, false, true); });
    picked = Object.create(null);
    if (!quiet) paintBulk();
  }

  /* Rows arriving after a selection was made come in unchecked, and a row that
     left the list takes its id with it. */
  function syncPicks() {
    var live = Object.create(null);
    pickBoxes().forEach(function (box) {
      var id = box.dataset.pick;
      if (picked[id] === undefined) return;
      live[id] = picked[id];
      box.checked = true;
      var row = box.closest('.row');
      if (row) row.classList.add('is-picked');
    });
    picked = live;
    paintBulk();
  }

  function onPickChange(box) {
    var boxes = pickBoxes();
    var at = boxes.indexOf(box);
    setPicked(box, box.checked, true);
    if (shiftHeld && lastPicked >= 0 && at >= 0 && at !== lastPicked) {
      var from = Math.min(at, lastPicked);
      var to = Math.max(at, lastPicked);
      for (var i = from; i <= to; i += 1) {
        var row = boxes[i].closest('.row');
        if (row && !row.hidden) setPicked(boxes[i], box.checked, true);
      }
      say((box.checked ? 'Selected ' : 'Cleared ') + (to - from + 1) + ', ' +
          pickCount() + ' selected in all');
    }
    shiftHeld = false;
    lastPicked = at;
    paintBulk();
  }

  function showStage(next) {
    stage = next;
    $$('.bulk__step', bulk).forEach(function (step) {
      step.hidden = step.dataset.step !== next;
    });
  }

  /* One line for outcomes, carried across every step of the bar. `tone` marks
     the ones that are failures; a batch that was capped is not one. */
  function bulkNote(text, tone) {
    var note = $('#bulk-note');
    if (!note) return;
    note.textContent = text || '';
    note.hidden = !text;
    if (tone) note.setAttribute('data-tone', tone);
    else note.removeAttribute('data-tone');
  }

  function paintStores() {
    var select = $('#bulk-store');
    if (!select) return;
    var list = [];
    Object.keys(picked).forEach(function (id) {
      picked[id].forEach(function (store) {
        if (list.indexOf(store) === -1) list.push(store);
      });
    });
    list.sort();
    if (select.dataset.list === list.join(',')) return;
    var chosen = select.value;
    select.dataset.list = list.join(',');
    select.textContent = '';
    list.forEach(function (id) {
      var option = document.createElement('option');
      option.value = id;
      option.textContent = storeLabel(id);
      select.appendChild(option);
    });
    if (list.indexOf(chosen) !== -1) select.value = chosen;
  }

  function paintBulk() {
    if (!bulk) return;
    var n = pickCount();
    var count = $('#bulk-n');
    if (count) count.textContent = String(n);
    paintStores();
    /* A selection emptied while the confirm is armed disarms it: the sentence
       on screen would otherwise name a count that no longer exists. What has
       already been sent stays on screen either way, because that is where the
       control that stops it lives. */
    if (n === 0 && stage !== 'sent') {
      showStage('pick');
      bulk.hidden = true;
    } else {
      bulk.hidden = false;
    }
    /* The bar floats over the list, so the list is given room to be scrolled
       clear of it. The space goes below everything on screen, which is why
       adding it moves nothing. */
    document.body.classList.toggle('has-bulk', !bulk.hidden);
  }

  function armBulk() {
    var select = $('#bulk-store');
    var store = select ? select.value : '';
    var ids = idsFor(store);
    if (!ids.length) return;
    var others = pickCount() - ids.length;
    var ask = 'Confirm ' + ids.length + (ids.length === 1 ? ' claim at ' : ' claims at ') +
      storeLabel(store) + '. Each one goes through on your say-so rather than on a' +
      ' confidence match, so ' + (ids.length === 1 ? 'one track is' : ids.length + ' tracks are') +
      ' bought and filed.';
    if (others) {
      ask += ' The other ' + others + (others === 1 ? ' selected track is' : ' selected tracks are') +
        ' listed at a different store. Confirm those separately.';
    }
    var line = $('#bulk-ask');
    var go = $('#bulk-go');
    if (line) line.textContent = ask;
    if (go) go.textContent = 'Confirm ' + ids.length;
    bulkNote('');
    showStage('ask');
    armedAt = Date.now();
    /* Focus lands on the way out, never on the confirm. A held Enter or a
       stray one has to land somewhere, and it must not land on money. */
    var back = $('#bulk-back');
    if (back) back.focus();
    say(ask);
  }

  function fireBulk(repeat) {
    /* Neither a held key nor a second press inside half a second spends
       anything: both are the same finger, not a second decision. */
    if (repeat || Date.now() - armedAt < 500) return;
    var select = $('#bulk-store');
    var store = select ? select.value : '';
    var ids = idsFor(store);
    if (!ids.length) return;
    var go = $('#bulk-go');
    if (go) go.disabled = true;
    fetch('/api/claim/confirm', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'libwish' },
      body: JSON.stringify({ track_ids: ids.map(Number), store: store })
    }).then(function (res) {
      return res.json().then(function (body) { return { status: res.status, ok: res.ok, body: body }; },
                             function () { return { status: res.status, ok: res.ok, body: null }; });
    }).catch(function () {
      return { status: 0, ok: false, body: null };
    }).then(function (answer) {
      if (go) go.disabled = false;
      if (!answer.ok || !answer.body || !answer.body.ok) {
        bulkFailed(answer.status, answer.body);
        return;
      }
      /* The server caps a batch and says how many it took. The tail stays
         selected rather than being dropped quietly, because a bulk action
         that loses part of its list reads exactly like one that worked. */
      var n = typeof answer.body.confirmed === 'number' ? answer.body.confirmed : ids.length;
      var taken = ids.slice(0, n);
      sentIds = taken.slice();
      taken.forEach(function (id) {
        var box = rack.querySelector('.pick[data-pick="' + id + '"]');
        if (box) setPicked(box, false, true);
        else delete picked[id];
      });
      var left = ids.length - n;
      bulkNote(left
        ? 'The first ' + n + ' of ' + ids.length + ' went through. The other ' + left +
          ' are still selected: confirm again to send them.'
        : '');
      var sent = $('#bulk-sent');
      if (sent) {
        sent.textContent = n + (n === 1 ? ' claim is queued at ' : ' claims are queued at ') +
          storeLabel(store) + '. Stopping the queue cancels every claim that has not started yet.';
      }
      showStage('sent');
      paintBulk();
      var stop = $('#bulk-stop');
      if (stop) { stop.hidden = false; stop.focus(); }
      say(n + ' claims queued at ' + storeLabel(store));
    });
  }

  /* Says what happened and what to do, and does not apologise. The selection
     is left alone so the same press can be tried again. */
  function bulkFailed(status, body) {
    var unknown = body && body.unknown ? body.unknown.length : 0;
    var text;
    if (!status) {
      text = 'Nothing was claimed. The server did not answer. Check the connection and' +
             ' try again.';
    } else if (unknown) {
      text = 'Nothing was claimed. ' + unknown + ' of the selected tracks are no longer on' +
             ' the server. Reload the page and select again.';
    } else if (body && body.error) {
      text = 'Nothing was claimed. The server said: ' + body.error + '.';
    } else {
      text = 'Nothing was claimed. The server answered ' + status + ' for the bulk confirm.' +
             ' Try again, or claim these tracks one at a time.';
    }
    showStage('pick');
    bulkNote(text, 'flag');
    say(text);
  }

  function stopBulk() {
    var ids = sentIds.slice();
    sentIds = [];
    var sent = $('#bulk-sent');
    var stop = $('#bulk-stop');
    if (stop) stop.hidden = true;
    if (sent) sent.textContent = 'Stopping ' + ids.length + '.';
    var chain = Promise.resolve();
    ids.forEach(function (id) {
      chain = chain.then(function () {
        return fetch('/api/claim/' + id + '/cancel', {
          method: 'POST',
          credentials: 'same-origin',
          headers: { 'X-Requested-With': 'libwish' }
        }).catch(function () { /* the row's own plate reports what survived */ });
      });
    });
    chain.then(function () {
      var text = 'Stopped. A claim already running finishes on its own and reports on its row.';
      if (sent) sent.textContent = text;
      say(text);
    });
  }

  function dismissBulk() {
    sentIds = [];
    showStage('pick');
    bulkNote('');
    paintBulk();
  }

  /* ------------------------------------------------------------------ *
   * connection
   * ------------------------------------------------------------------ */

  var source = null;
  var countdown = null;

  function offline(on) {
    var banner = $('#banner-offline');
    if (!banner) return;
    banner.hidden = !on;
    if (!on) {
      if (countdown) { window.clearInterval(countdown); countdown = null; }
      return;
    }
    var left = 8;
    var counter = $('#retry-count');
    if (counter) counter.textContent = String(left);
    if (countdown) window.clearInterval(countdown);
    countdown = window.setInterval(function () {
      left = left > 1 ? left - 1 : 8;
      if (counter) counter.textContent = String(left);
    }, 1000);
    say('Live updates disconnected. The list on screen is the last one received.');
  }

  function connect() {
    if (source) source.close();
    source = new EventSource(document.body.dataset.events || '/api/events');

    source.addEventListener('open', function () { offline(false); });
    source.addEventListener('error', function () {
      if (source.readyState === EventSource.CLOSED || source.readyState === EventSource.CONNECTING) {
        offline(true);
      }
    });

    var on = function (name, fn) {
      source.addEventListener(name, function (e) {
        var data = {};
        try { data = JSON.parse(e.data); } catch (err) { return; }
        offline(false);
        fn(data);
      });
    };

    /* Counted here rather than inside the handlers below. Two of them return
       early when the track is not on this page, which is exactly the case the
       tabs exist for: a claim finishing while the ignored list is open moves a
       track between two views the reader is not looking at, and the numbers
       have to follow it. */
    on('track.added', function (d) { onTrackAdded(d); countsChanged(); });
    on('track.removed', function (d) { onTrackRemoved(d); countsChanged(); });
    on('track.updated', function (d) { onTrackUpdated(d); countsChanged(); });
    on('track.source_added', function (d) { refreshRow(d.id); });
    on('job.started', function (d) { if (d.track_id) refreshRow(d.track_id); });
    on('job.progress', onJobProgress);
    /* The sweep has no track_id, so the row-oriented handlers above ignore it
       and it needs its own listener rather than a branch inside theirs. */
    on('job.progress', onSyncProgress);
    /* The move between tabs is `track.updated`'s: it carries the new status,
       so `onTrackUpdated` takes the row off the list it has left and puts it
       on the one it has joined. This is the plate and the numbers catching up
       afterwards, and it stays separate because a job can finish without any
       status having changed. Both are safe to arrive in either order: an
       insert skips a row already present, and a refresh of a row that is not
       there does nothing. */
    on('job.finished', function (d) {
      if (d.track_id) refreshRow(d.track_id);
      countsChanged();
    });
    on('job.failed', function (d) { if (d.track_id) refreshRow(d.track_id); });
    /* A sweep that dies mid-run would otherwise leave the button spinning
       forever, which reads as "still working" rather than "it stopped". */
    on('job.failed', function (d) {
      if (d.kind === 'sync') syncBusy(false, d.error || 'The sync stopped.');
    });
    on('credential.updated', onCredential);
    on('provider.state', onCredential);
    on('scan.requested', function (d) {
      say(d.ok ? 'Sources checked' : 'Could not check sources');
    });
  }

  /* A page kept in the back/forward cache keeps its EventSource open, and a
     browser allows only about six connections to one origin. Switching between
     tabs quickly leaves several cached pages each holding one, and once they
     are all held the next navigation cannot get a connection at all: the site
     stops loading while the server sits idle and healthy.

     pagehide is the hook that fires for both a real unload and a move into the
     cache, so the stream is given up either way. A page restored from the cache
     gets a fresh one, because the old connection is gone and its own state is
     stale by then anyway. */
  window.addEventListener('pagehide', function () {
    if (source) { source.close(); source = null; }
  });

  window.addEventListener('pageshow', function (e) {
    if (!e.persisted) return;
    if (window.EventSource) connect();
    /* The cached document still shows whatever it showed when it was put
       away, and a sweep that finished in between sent its last phase to a
       stream nobody was holding. */
    restoreSync();
  });

  /* Names the consequence, not just the fault: "needs reconnecting" alone does
     not tell you that loves have stopped arriving. */
  function onCredential(d) {
    var banner = $('#banner-credential');
    var text = $('#credential-text');
    if (!banner || !text) return;
    var broken = d.status === 'expired' || d.status === 'revoked' || d.state === 'auth_expired' ||
                 d.state === 'error';
    if (!broken) { banner.hidden = true; return; }
    var who = d.provider || d.provider_id || d.id || 'A source';
    text.textContent = who.charAt(0).toUpperCase() + who.slice(1) +
      ' needs reconnecting. New loves are not arriving.';
    banner.hidden = false;
    say(text.textContent);
  }

  /* ------------------------------------------------------------------ *
   * preview
   * ------------------------------------------------------------------ */

  function stopPreview() {
    if (!audio) return;
    audio.pause();
    $$('.preview[aria-pressed="true"]').forEach(function (b) {
      b.setAttribute('aria-pressed', 'false');
    });
    playingId = null;
  }

  function preview(button) {
    var id = button.dataset.preview;
    if (playingId === id) { stopPreview(); return; }
    stopPreview();
    button.setAttribute('aria-pressed', 'true');
    playingId = id;
    fetch('/api/preview/' + id, { credentials: 'same-origin' })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (!d.url || playingId !== id) { stopPreview(); return; }
        audio.src = d.url;
        audio.play().catch(function () { stopPreview(); });
      })
      .catch(stopPreview);
  }

  /* ------------------------------------------------------------------ *
   * keyboard
   * ------------------------------------------------------------------ */

  /* A roving tabindex, so leaving a 160-row list costs one Tab rather than
     one hundred and sixty. */
  function focusRow(row) {
    if (!row) return;
    $$('.row[tabindex="0"]', rack).forEach(function (r) { r.tabIndex = -1; });
    row.tabIndex = 0;
    row.focus();
  }

  function visibleRows() {
    return $$('.row', rack).filter(function (r) { return !r.hidden; });
  }

  function typing(el) {
    if (!el) return false;
    var tag = el.tagName;
    return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || el.isContentEditable;
  }

  function onKey(e) {
    if (e.key === 'Escape') {
      /* Backing out of the confirm comes first. It is the only one of these
         with money behind it, and it is the one a reader reaches for. */
      if (bulk && stage === 'ask') {
        showStage('pick');
        var arm = $('#bulk-arm');
        if (arm) arm.focus();
        say('Confirm cancelled. The selection is still there.');
        return;
      }
      if (pickCount()) {
        clearPicks();
        say('Selection cleared');
      }
      return;
    }
    if (typing(e.target) || !rack) return;

    var row = e.target.closest ? e.target.closest('.row') : null;

    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      var rows = visibleRows();
      if (!rows.length) return;
      e.preventDefault();
      var at = row ? rows.indexOf(row) : -1;
      var next = e.key === 'ArrowDown' ? Math.min(at + 1, rows.length - 1) : Math.max(at - 1, 0);
      focusRow(rows[at === -1 ? 0 : next]);
      return;
    }

    if (!row) return;

    /* Enter runs the row's one primary action. The refusal override is
       deliberately not marked primary, so it can never be reached this way. */
    if (e.key === 'Enter') {
      /* A row can hold two primaries at once, one of which Alpine has hidden:
         Buy before the shop is opened, "I bought it" after. Take the one on
         screen, never the one behind it. */
      var primary = $$('[data-primary]', row).filter(function (el) {
        return el.offsetParent !== null;
      })[0];
      if (primary) { e.preventDefault(); primary.click(); }
      return;
    }
    /* Selecting from the keyboard costs one key on the focused row, so a
       hundred and sixty of them are reachable without a pointer. */
    if (e.key === 's' || e.key === 'S') {
      var box = row.querySelector('.pick');
      if (box) {
        e.preventDefault();
        setPicked(box, !box.checked);
        lastPicked = pickBoxes().indexOf(box);
        say((box.checked ? 'Selected. ' : 'Cleared. ') + pickCount() + ' selected in all');
      }
      return;
    }
    if (e.key === 'c') {
      var claim = row.querySelector('[data-act="claim"]');
      if (claim) { e.preventDefault(); claim.click(); }
      return;
    }
    if (e.key === 'x') {
      var ignore = row.querySelector('[data-act="ignore"]');
      if (ignore) { e.preventDefault(); ignore.click(); }
    }
  }

  /* ------------------------------------------------------------------ *
   * start
   * ------------------------------------------------------------------ */

  /* ------------------------------------------------------------------ *
   * sync purchases
   *
   * One button for "I bought several things, sort them out". It starts a
   * single sweep job; the queued claims then report themselves through the
   * ordinary job stream like any other claim, so nothing here has to follow
   * them. What it does own is the button's own state, because a sweep that
   * looks idle while it is running invites a second press.
   * ------------------------------------------------------------------ */

  function syncBusy(on, said) {
    var button = $('#sync-purchases');
    if (!button) return;
    button.disabled = !!on;
    button.classList.toggle('is-busy', !!on);
    if (said !== undefined) {
      var out = $('#sync-said');
      if (out) out.textContent = said;
    }
  }

  function startSync() {
    syncBusy(true, 'Reading your purchases...');
    fetch('/api/sync', { method: 'POST', headers: { 'X-Requested-With': 'libwish' } })
      .then(function (r) { return r.json().then(function (b) { return { ok: r.ok, body: b }; }); })
      .then(function (res) {
        if (!res.ok) {
          /* 409 means one is already running, which is not an error worth a
             red banner: the answer is to wait, and the stream will say when. */
          syncBusy(res.body && res.body.job_id ? true : false, res.body.error || 'Could not start.');
          return;
        }
        say('Sync started.');
      })
      .catch(function () { syncBusy(false, 'Could not reach the server.'); });
  }

  /* ------------------------------------------------------------------ *
   * importing files bought elsewhere
   *
   * iTunes hands over a file and keeps no record worth reading, so the file is
   * the purchase. This uploads whatever was dropped or chosen and reports on
   * each one: the rows themselves arrive over the ordinary event stream, the
   * same way a claim's do, so nothing here inserts anything into the list.
   *
   * The window is the drop target. A zone to aim at is one more thing to find,
   * and the files are already being dragged over the page from a Finder window.
   * ------------------------------------------------------------------ */

  function importBusy(on) {
    var label = $('.masthead__add');
    var input = $('#import-files');
    if (label) label.classList.toggle('is-busy', !!on);
    if (input) input.disabled = !!on;
  }

  /* One sentence for the whole drop, then a line per file that could not be
     filed. The counts come first because that is the question ("did it work?");
     the failures are what is left to do something about. */
  function importSaid(body) {
    var results = (body && body.results) || [];
    var filed = results.filter(function (r) { return r.ok && !r.already_held; });
    var held = results.filter(function (r) { return r.ok && r.already_held; });
    var failed = results.filter(function (r) { return !r.ok; });
    var parts = [];
    if (filed.length) parts.push('Added ' + filed.length + (filed.length === 1 ? ' track.' : ' tracks.'));
    if (held.length) parts.push(held.length + (held.length === 1 ? ' was' : ' were') + ' already in your library.');
    if (failed.length) parts.push(failed.length + ' could not be read.');
    if (!parts.length) parts.push('Nothing was sent.');
    return { said: parts.join(' '), failed: failed };
  }

  function showImported(said, failed) {
    var box = $('#imported');
    var line = $('#imported-said');
    var list = $('#imported-failed');
    if (!box || !line || !list) return;
    line.textContent = said;
    list.textContent = '';
    /* The message names its own file, because the same sentence is what an API
       caller gets back with no list around it to say which file it is about.
       Printing the name again beside it read as a stutter. */
    (failed || []).forEach(function (r) {
      var li = document.createElement('li');
      li.textContent = r.msg || (r.file + ' could not be read.');
      list.appendChild(li);
    });
    box.hidden = false;
    say(said);
  }

  function sendFiles(files) {
    var list = Array.prototype.slice.call(files || []);
    if (!list.length) return;
    var form = new FormData();
    list.forEach(function (f) { form.append('files', f, f.name); });
    importBusy(true);
    showImported('Adding ' + list.length + (list.length === 1 ? ' file...' : ' files...'), []);
    fetch('/api/import', {
      method: 'POST',
      headers: { 'X-Requested-With': 'libwish' },
      body: form
    })
      .then(function (r) {
        /* A 413 is answered by the server before the body is read, and its
           reply is an HTML page rather than the JSON every other route sends,
           so it is named here rather than left to fail as a parse error. */
        if (r.status === 413) throw new Error('That is more than one upload can carry.');
        return r.json();
      })
      .then(function (body) {
        var out = importSaid(body);
        showImported(out.said, out.failed);
      })
      .catch(function (err) {
        showImported(err.message || 'Could not reach the server.', []);
      })
      .then(function () { importBusy(false); });
  }

  /* Drag events fire per element, so entering a child looks like leaving the
     parent. Counting them is what keeps the overlay from flickering off as the
     pointer crosses a row on its way down the page. */
  var dragDepth = 0;

  function draggingFiles(e) {
    var dt = e.dataTransfer;
    if (!dt) return false;
    var types = dt.types || [];
    return Array.prototype.indexOf.call(types, 'Files') !== -1;
  }

  function showDrop(on) {
    var zone = $('#drop');
    if (zone) zone.hidden = !on;
  }

  function wireImport() {
    var input = $('#import-files');
    if (input) {
      input.addEventListener('change', function () {
        sendFiles(input.files);
        /* Cleared so that choosing the same file twice still fires a change.
           Dropping a file, deciding it went to the wrong place and picking it
           again is a real sequence, and silence is the worst answer to it. */
        input.value = '';
      });
    }

    var dismiss = $('#imported-dismiss');
    if (dismiss) {
      dismiss.addEventListener('click', function () {
        var box = $('#imported');
        if (box) box.hidden = true;
      });
    }

    window.addEventListener('dragenter', function (e) {
      if (!draggingFiles(e)) return;
      e.preventDefault();
      dragDepth += 1;
      showDrop(true);
    });

    window.addEventListener('dragover', function (e) {
      if (!draggingFiles(e)) return;
      /* Without this the browser navigates to the file, which throws the page
         away along with whatever was mid-decision on it. */
      e.preventDefault();
      e.dataTransfer.dropEffect = 'copy';
    });

    window.addEventListener('dragleave', function () {
      dragDepth = Math.max(0, dragDepth - 1);
      if (!dragDepth) showDrop(false);
    });

    window.addEventListener('drop', function (e) {
      if (!draggingFiles(e)) return;
      e.preventDefault();
      dragDepth = 0;
      showDrop(false);
      sendFiles(e.dataTransfer.files);
    });

    /* A drag abandoned outside the window does not always come back as a
       leave, and the count would then never reach zero. What is at stake is
       not tidiness: this element covers the entire page, so one missed event
       leaves the application unusable until a reload. */
    window.addEventListener('dragend', function () {
      dragDepth = 0;
      showDrop(false);
    });

    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && dragDepth) {
        dragDepth = 0;
        showDrop(false);
      }
    });
  }

  /* One sentence per phase, from the phase name and its own payload. Written
     once and called from two places: the live stream, and the restore below
     that reads the same pair back off the job row after a navigation. Two
     copies would drift, and the way they would drift is a reader seeing one
     wording while watching and a different one after changing tabs. */
  function syncSaid(phase, data) {
    if (phase === 'session') return 'Reading your purchases...';
    if (phase === 'enumerate') return 'Read ' + (data.purchases || 0) + ' purchases.';
    if (phase === 'match') return 'Matching ' + (data.purchases || 0) + ' purchases...';
    if (phase !== 'queue') return 'Reading your purchases...';

    var queued = data.queued || 0;
    var near = data.near_misses || 0;
    var parts = [];
    parts.push(queued ? 'Filing ' + queued + (queued === 1 ? ' purchase.' : ' purchases.')
                      : 'Nothing new to file.');
    if (near) parts.push(near + (near === 1 ? ' was too close to call.' : ' were too close to call.'));
    (data.shops_skipped || []).forEach(function (s) {
      parts.push(s.shop + ' was skipped: ' + s.why + '.');
    });
    return parts.join(' ');
  }

  /* Reads the sweep's own phases off the job stream. The counts it reports at
     the end are the only place a reader learns that nothing matched, which is
     a different answer from nothing having been bought. */
  function onSyncProgress(data) {
    if (data.kind !== 'sync') return;
    if (data.phase !== 'queue' && data.phase !== 'enumerate' && data.phase !== 'match') return;
    var said = syncSaid(data.phase, data);
    syncBusy(data.phase !== 'queue', said);
    if (data.phase === 'queue') say(said);
  }

  /* How long a finished sweep keeps explaining itself. The line under the
     button answers "what happened to the thing I just pressed", and after a
     while there is no longer a thing anyone just pressed: coming back to the
     page tomorrow and reading "Filing 5 purchases." would describe yesterday
     in the present tense. */
  var SYNC_RECENT_S = 600;

  /* Every tab here is a real link, so a sweep started on one page is watched
     from a document that never saw it start. The button state and the line
     under it are therefore taken from the job row rather than from anything
     this page remembers, which also makes them survive a reload and come back
     right in a second window. */
  function restoreSync() {
    if (!$('#sync-purchases')) return;
    fetch('/api/sync', { headers: { 'X-Requested-With': 'libwish' }, credentials: 'same-origin' })
      .then(function (res) { return res.ok ? res.json() : null; })
      .then(function (job) {
        if (!job || !job.state) return;
        if (job.state === 'queued' || job.state === 'running') {
          /* Still going, so the stream will carry it from here. */
          syncBusy(true, syncSaid(job.phase, job.progress || {}));
          return;
        }
        if (job.state === 'failed' || job.state === 'interrupted') {
          syncBusy(false, job.error || 'The sync stopped.');
          return;
        }
        var age = Math.floor(Date.now() / 1000) - (job.finished_at || 0);
        if (job.finished_at && age <= SYNC_RECENT_S) {
          syncBusy(false, syncSaid(job.phase, job.progress || {}));
        }
      })
      .catch(function () { /* the button stays as the page rendered it */ });
  }

  function start() {
    rack = $('#rack');
    announcer = $('#announcer');
    audio = $('#preview-audio');
    more = $('#more');
    bulk = $('#bulk');

    watchCovers();
    paintMore();

    var syncBtn = $('#sync-purchases');
    if (syncBtn) syncBtn.addEventListener('click', startSync);
    restoreSync();
    wireImport();

    var island = $('#claim-stages');
    if (island) {
      try {
        var parsed = JSON.parse(island.textContent);
        stages = parsed.stages || {};
        phaseOrder = parsed.order || [];
      } catch (e) { /* the plate still shows the phase name the event carried */ }
    }

    document.addEventListener('click', function (e) {
      if (!e.target.closest) return;

      var button = e.target.closest('[data-preview]');
      if (button) { e.preventDefault(); preview(button); return; }

      /* Shift on the way in, read on the way out: the checkbox's own change
         event carries no modifier, and a range is how selecting forty rows
         stops being forty presses. */
      if (e.target.closest('.pick') || e.target.closest('label.row__pick')) {
        shiftHeld = e.shiftKey;
      }

      /* The row's own surface selects it. An 18px checkbox is a small target
         for something the reader does forty times in a sitting, and the rest
         of the row was doing nothing on click.

         What the row can do instead is listed by name rather than by position:
         it holds a link, three buttons, a menu, a checkbox in its own label
         and a panel that opens under it, and every one of those already means
         something else. The checkbox is driven rather than set directly, so a
         click through the row picks up shift-range selection from the same
         code path the checkbox itself uses. */
      var pickable = e.target.closest('.row--pickable');
      if (pickable &&
          !e.target.closest('a, button, input, label, select, .menu, .row__detail')) {
        /* A drag that ended up selecting a title is not a click on the row.
           Toggling here would undo the selection and surprise the reader. */
        if (String(window.getSelection() || '')) return;
        var rowBox = pickable.querySelector('.pick');
        if (rowBox) {
          /* The box is toggled and the same handler called, rather than
             clicked. A synthetic click reaches this listener again before the
             browser gets round to toggling anything, and the second pass sees
             an event carrying no shift key and clears the modifier the range
             was about to be read with. */
          shiftHeld = e.shiftKey;
          rowBox.checked = !rowBox.checked;
          onPickChange(rowBox);
          return;
        }
      }

      if (e.target.closest('#more-load')) {
        e.preventDefault();
        loadMore(false);
        return;
      }
      if (e.target.closest('#bulk-clear')) { clearPicks(); say('Selection cleared'); return; }
      if (e.target.closest('#bulk-arm')) { armBulk(); return; }
      if (e.target.closest('#bulk-back')) {
        showStage('pick');
        var arm = $('#bulk-arm');
        if (arm) arm.focus();
        return;
      }
      if (e.target.closest('#bulk-go')) { fireBulk(false); return; }
      if (e.target.closest('#bulk-stop')) { stopBulk(); return; }
      if (e.target.closest('#bulk-done')) { dismissBulk(); return; }

      if (e.target.closest('#newpill-show')) {
        var ids = pending.slice();
        pending = [];
        showPill();
        insertRows(ids);
        return;
      }
      if (e.target.closest && e.target.closest('#retry-now')) { connect(); return; }
    });

    /* Buy opens in a new tab by default: nothing above touches `target` or
       `rel` on the anchor, and that stays true here too. What changes is
       whether the wishlist browser extension is on the page, stamped as
       `data-lw-extension="1"` on <html> at document_start before this file
       runs. With the stamp present the extension draws a "back to the
       wishlist" bar on the store page, which makes navigating the same tab
       the better move: the back button then hands the reader this exact page
       out of bfcache, scroll position and every lazily loaded row
       included. Without the stamp there is no bar to get back with, so
       the new tab stands.

       A reader who asks for a new tab on purpose, by a modifier-click or a
       non-primary button, still gets one either way; this only redirects a
       plain left click.

       Delegated on document rather than bound to each anchor, because most
       buy anchors do not exist yet when this file runs: they arrive by an
       htmx swap or off the live event stream for as long as the tab stays
       open, and a listener bound at load time would never see them. */
    document.addEventListener('click', function (e) {
      if (!e.target.closest) return;
      var link = e.target.closest('a[data-buy]');
      if (!link) return;
      if (document.documentElement.dataset.lwExtension !== '1') return;
      if (e.defaultPrevented) return;
      if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
      e.preventDefault();
      window.location.href = link.href;
    });

    if (audio) audio.addEventListener('ended', stopPreview);

    document.addEventListener('change', function (e) {
      var box = e.target.closest ? e.target.closest('.pick') : null;
      if (box) onPickChange(box);
    });

    if (bulk) {
      var go = $('#bulk-go');
      if (go) {
        /* A key held down repeats. Every repeat after the first is the same
           press, and none of them may spend anything. */
        go.addEventListener('keydown', function (e) {
          if (e.repeat && (e.key === 'Enter' || e.key === ' ' || e.key === 'Spacebar')) {
            e.preventDefault();
          }
        });
      }
    }

    if (rack) {
      rack.addEventListener('focusin', function (e) {
        var row = e.target.closest('.row');
        if (row) {
          $$('.row[tabindex="0"]', rack).forEach(function (r) { if (r !== row) r.tabIndex = -1; });
          row.tabIndex = 0;
        }
      });
      var first = $('.row', rack);
      if (first) first.tabIndex = 0;
    }

    document.addEventListener('keydown', onKey);


    if (window.EventSource) connect();
    registerWorker();
  }

  // Registered after the page is interactive, so the install never competes
  // with the first render for bandwidth.
  //
  // `isSecureContext` is the whole story on a LAN: a worker is refused over
  // plain http to anything but localhost, and with no worker the browser will
  // not offer to install. Served over http://<host>:<port> this is simply a
  // website, which is why the failure is logged rather than surfaced.
  function registerWorker() {
    if (!('serviceWorker' in navigator) || !window.isSecureContext) return;
    window.addEventListener('load', function () {
      navigator.serviceWorker.register('/sw.js').catch(function (err) {
        console.warn('offline support unavailable:', err);
      });
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start);
  } else {
    start();
  }
})();
