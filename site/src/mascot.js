/*
 * Postbot — the StudyBuddy mascot.
 *
 * A vanilla-JS port of page-mascot (https://github.com/nilbuild/page-mascot,
 * MIT © Kamran Ahmed). Same mechanics: two 3×3 sprite sheets, the pointer's
 * angle picks a head direction, a click shows a reaction, one
 * background-position step and no animation library. Ported because this
 * project has no build step by design — the landing page is a single static
 * file with no npm pipeline.
 *
 * Sprite sheets: /mascots/postbot-directions.webp and /mascots/postbot-reactions.webp
 * (page-mascot character set, MIT).
 */
(function () {
  'use strict';

  var DIRECTIONS = [
    'up-left', 'up', 'up-right',
    'left', 'center', 'right',
    'down-left', 'down', 'down-right',
  ];
  var REACTIONS = [
    'blink', 'heart', 'sparkle', 'surprised', 'wink',
    'bashful', 'sleepy', 'dizzy', 'delighted',
  ];
  // Clockwise from the right, matching atan2 with y pointing down.
  var CLOCKWISE = [
    'right', 'down-right', 'down', 'down-left',
    'left', 'up-left', 'up', 'up-right',
  ];
  var SECTOR = (Math.PI * 2) / CLOCKWISE.length;
  var HYSTERESIS = 0.12;
  var DEAD_ZONE = 70;
  var PAYOFFS = ['heart', 'sparkle', 'delighted'];
  var BOOP_PAYOFF = 120;
  var BOOP_END = 560;
  var DIZZY_AFTER = 4;
  var DIZZY_WINDOW = 1600;
  var DIZZY_END = 1100;
  var SQUASH = [
    { transform: 'scale(1, 1)', easing: 'ease-in' },
    { transform: 'scale(1.10, 0.86)', offset: 0.18, easing: 'ease-out' },
    { transform: 'scale(0.95, 1.08)', offset: 0.45, easing: 'ease-in-out' },
    { transform: 'scale(1.03, 0.97)', offset: 0.72, easing: 'ease-in-out' },
    { transform: 'scale(1, 1)' },
  ];

  // background-size 300% makes each cell a clean 0/50/100% step on both axes.
  function cell(index) {
    return { backgroundPosition: (index % 3) * 50 + '% ' + Math.floor(index / 3) * 50 + '%' };
  }
  function wrap(angle) {
    return Math.atan2(Math.sin(angle), Math.cos(angle));
  }

  function create(opts) {
    var directions = opts.directions;
    var reactions = opts.reactions;
    var size = opts.size || 140;
    var label = opts.label || 'mascot';

    var button = document.createElement('button');
    button.type = 'button';
    button.setAttribute('aria-label', 'Boop ' + label);
    button.className = 'postbot';
    button.style.cssText =
      'position:relative;display:block;flex:none;width:' + size + 'rem;height:' + size + 'rem;' +
      'padding:0;border:0;background:transparent;appearance:none;cursor:pointer;' +
      'user-select:none;-webkit-user-select:none;';

    var squash = document.createElement('span');
    squash.style.cssText =
      'position:relative;display:block;width:100%;height:100%;transform-origin:50% 78%;';

    var layerBase =
      'position:absolute;inset:0;background-size:300% 300%;background-repeat:no-repeat;';
    var dirLayer = document.createElement('span');
    dirLayer.style.cssText = layerBase + 'background-image:url(' + directions + ');opacity:1;';
    var reactLayer = document.createElement('span');
    reactLayer.style.cssText = layerBase + 'background-image:url(' + reactions + ');opacity:0;';
    squash.appendChild(dirLayer);
    squash.appendChild(reactLayer);
    button.appendChild(squash);

    var direction = 'center';
    var timers = [];
    var boops = { count: 0, at: 0 };

    function clearTimers() {
      timers.forEach(clearTimeout);
      timers = [];
    }
    function later(ms, fn) {
      timers.push(setTimeout(fn, ms));
    }
    function setCell(layer, index) {
      var pos = cell(index);
      layer.style.backgroundPosition = pos.backgroundPosition;
    }
    function setDirection(name) {
      direction = name;
      setCell(dirLayer, DIRECTIONS.indexOf(name));
    }
    function showReaction(name, holdMs) {
      if (!name) {
        reactLayer.style.opacity = '0';
        dirLayer.style.opacity = '1';
        return;
      }
      setCell(reactLayer, REACTIONS.indexOf(name));
      reactLayer.style.opacity = '1';
      dirLayer.style.opacity = '0';
      if (holdMs) {
        later(holdMs, function () { showReaction(null); });
      }
    }

    // Pointer tracking: fine pointers only, same hysteresis as the original.
    if (window.matchMedia('(hover: hover) and (pointer: fine)').matches) {
      var sector = -1;
      var pointer = null;
      var aim = function () {
        if (!button.isConnected || !pointer) return;
        var box = button.getBoundingClientRect();
        var dx = pointer.x - (box.left + box.width / 2);
        var dy = pointer.y - (box.top + box.height / 2);
        if (Math.hypot(dx, dy) < DEAD_ZONE) {
          sector = -1;
          setDirection('center');
          return;
        }
        var angle = Math.atan2(dy, dx);
        if (sector !== -1 && Math.abs(wrap(angle - sector * SECTOR)) < SECTOR / 2 + HYSTERESIS) {
          return;
        }
        sector = (Math.round(angle / SECTOR) + CLOCKWISE.length) % CLOCKWISE.length;
        setDirection(CLOCKWISE[sector]);
      };
      window.addEventListener('pointermove', function (e) {
        pointer = { x: e.clientX, y: e.clientY };
        aim();
      }, { passive: true });
      window.addEventListener('scroll', aim, { passive: true });
    }

    function boop() {
      clearTimers();
      var now = Date.now();
      boops.count = now - boops.at < DIZZY_WINDOW ? boops.count + 1 : 1;
      boops.at = now;
      if (boops.count >= DIZZY_AFTER) {
        boops.count = 0;
        showReaction('dizzy');
        later(DIZZY_END, function () { showReaction(null); });
      } else {
        showReaction('blink');
        later(BOOP_PAYOFF, function () {
          showReaction(PAYOFFS[(boops.count - 1) % PAYOFFS.length]);
        });
        later(BOOP_END, function () { showReaction(null); });
      }
      if (!window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
        if (squash.animate) squash.animate(SQUASH, { duration: 420, easing: 'linear' });
      }
    }
    button.addEventListener('click', boop);

    return {
      el: button,
      // Driven by the chat: thinking → blink, success → delighted, failure → dizzy.
      react: function (name, holdMs) {
        clearTimers();
        showReaction(name, holdMs);
      },
    };
  }

  window.Postbot = { create: create };
})();
