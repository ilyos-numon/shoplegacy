/*
 * 가게유산 모션 헬퍼 (GSAP 3.12)
 * - 프레임워크 없이 쓰는 전역 스크립트. gsap.min.js 다음에 불러온다.
 *   <script src="/static/vendor/gsap.min.js"></script>
 *   <script src="/static/js/motion.js"></script>
 * - 동작 줄이기(prefers-reduced-motion) 설정이면 이동·크기 변화 없이 0.15초 투명도만 쓴다.
 * - GSAP이 없으면 모든 함수가 애니메이션 없이 상태만 바꾼다.
 */
(function (global) {
  'use strict';
  var g = global.gsap || null;
  var reduce = global.matchMedia && global.matchMedia('(prefers-reduced-motion: reduce)').matches;

  function fade(el, show, done) {
    if (!g) { if (el) el.hidden = !show; if (done) done(); return; }
    if (show) el.hidden = false;
    g.to(el, { opacity: show ? 1 : 0, duration: 0.15, onComplete: function () { if (!show) el.hidden = true; if (done) done(); } });
  }

  var M = {
    /** 페이지 진입. dir: 1 = 앞으로(오른쪽에서), -1 = 뒤로(왼쪽에서). 각 화면 JS의 DOMContentLoaded에서 호출 */
    enterPage: function (root, dir) {
      root = root || document.querySelector('main');
      if (!g || !root) return;
      dir = dir || Number(sessionStorage.getItem('gy-nav-dir') || 1);
      try { sessionStorage.removeItem('gy-nav-dir'); } catch (e) {}
      if (reduce) { g.from(root, { opacity: 0, duration: 0.15 }); return; }
      g.from(root, { x: 28 * dir, opacity: 0, duration: 0.42, ease: 'power3.out', clearProps: 'transform' });
      var items = root.querySelectorAll('[data-stagger]');
      if (items.length) g.from(items, { y: 10, opacity: 0, duration: 0.4, stagger: 0.05, ease: 'power2.out', delay: 0.15, clearProps: 'all' });
    },

    /** 다른 화면으로 이동하기 전에 나가는 애니메이션 후 location 변경 */
    leaveTo: function (href, dir) {
      try { sessionStorage.setItem('gy-nav-dir', String(dir || 1)); } catch (e) {}
      var root = document.querySelector('main');
      if (!g || !root || reduce) { location.href = href; return; }
      g.to(root, { x: -28 * (dir || 1), opacity: 0, duration: 0.22, ease: 'power2.in', onComplete: function () { location.href = href; } });
    },

    /** 아래에서 올라오는 시트. sheet와 scrim은 hidden 속성으로 시작 */
    openSheet: function (sheet, scrim) {
      sheet.hidden = false; if (scrim) scrim.hidden = false;
      if (!g) return;
      if (scrim) g.fromTo(scrim, { opacity: 0 }, { opacity: 1, duration: 0.25 });
      if (reduce) g.fromTo(sheet, { opacity: 0 }, { opacity: 1, duration: 0.15 });
      else g.fromTo(sheet, { yPercent: 105 }, { yPercent: 0, duration: 0.5, ease: 'expo.out' });
      var focusable = sheet.querySelector('input,select,textarea,button');
      if (focusable) focusable.focus({ preventScroll: true });
    },
    closeSheet: function (sheet, scrim, done) {
      var end = function () { sheet.hidden = true; if (scrim) scrim.hidden = true; if (done) done(); };
      if (!g) return end();
      if (scrim) g.to(scrim, { opacity: 0, duration: 0.25 });
      if (reduce) g.to(sheet, { opacity: 0, duration: 0.12, onComplete: end });
      else g.to(sheet, { yPercent: 105, duration: 0.3, ease: 'power2.in', onComplete: end });
    },

    /** "다음 할 일" 링. ring은 container 안 position:absolute 요소, target은 강조할 주 버튼 */
    coach: function (ring, target, container, label) {
      if (!ring) return;
      if (!target || target.offsetParent === null) { if (g) g.to(ring, { opacity: 0, duration: 0.15 }); else ring.style.opacity = 0; return; }
      var c = container.getBoundingClientRect(), r = target.getBoundingClientRect();
      var box = { left: r.left - c.left - 5, top: r.top - c.top - 5, width: r.width + 10, height: r.height + 10, opacity: 1 };
      if (label) ring.setAttribute('data-label', label);
      if (!g) { Object.keys(box).forEach(function (k) { ring.style[k] = box[k] + (k === 'opacity' ? '' : 'px'); }); return; }
      g.to(ring, Object.assign(box, { duration: reduce ? 0 : 0.5, ease: 'power3.inOut' }));
      if (!reduce) g.fromTo(ring, { boxShadow: '0 0 0 0 rgba(174,63,43,.35)' }, { boxShadow: '0 0 0 10px rgba(174,63,43,0)', duration: 1.1, delay: 0.5, repeat: 1 });
    },

    /** 인터뷰 질문 교체: 이전 질문 위로 사라지고 새 질문 아래에서 올라옴 */
    swapText: function (els, apply) {
      if (!g) { apply(); return; }
      g.to(els, { y: reduce ? 0 : -12, opacity: 0, duration: 0.2, ease: 'power1.in', onComplete: function () {
        apply();
        g.fromTo(els, { y: reduce ? 0 : 16, opacity: 0 }, { y: 0, opacity: 1, duration: 0.45, stagger: 0.06, ease: 'power3.out' });
      } });
    },

    /** 마이크 듣는 중 맥박. 반환값.kill()로 멈춘다. rings: 버튼 안 .ring 요소들 */
    micPulse: function (rings) {
      if (!g || reduce) return { kill: function () {} };
      var tl = g.timeline({ repeat: -1 });
      tl.fromTo(rings, { scale: 1, opacity: 0.55 }, { scale: 1.32, opacity: 0, duration: 1.4, ease: 'sine.out', stagger: 0.7 });
      return { kill: function () { tl.kill(); g.set(rings, { opacity: 0, scale: 1 }); } };
    },

    /** 도장 (사장님 승인 確認, 카드 공개 公開). stampEl은 position:absolute 원형 요소 */
    stamp: function (stampEl, text, done) {
      stampEl.textContent = text;
      if (!g) { if (done) done(); return; }
      if (reduce) { g.fromTo(stampEl, { opacity: 0 }, { opacity: 0.95, duration: 0.15, yoyo: true, repeat: 1, repeatDelay: 0.6, onComplete: done }); return; }
      g.timeline({ onComplete: done })
        .fromTo(stampEl, { opacity: 0, scale: 1.8, rotation: -4 }, { opacity: 0.95, scale: 1, rotation: -14, duration: 0.32, ease: 'back.out(2)' })
        .to(stampEl, { opacity: 0, duration: 0.5, delay: 0.7, ease: 'power1.in' });
    },

    /** 숫자 세기 (대시보드) */
    countUp: function (el, to) {
      if (!g || reduce) { el.textContent = to; return; }
      var o = { v: 0 };
      g.to(o, { v: to, duration: 0.9, ease: 'power1.out', onUpdate: function () { el.textContent = Math.round(o.v).toLocaleString('ko-KR'); } });
    },

    /** 진행 막대 채우기 (가게 6단계, 인터뷰 여섯 주제) */
    fillBar: function (el, ratio) {
      if (!g) { el.style.transform = 'scaleX(' + ratio + ')'; return; }
      g.to(el, { scaleX: ratio, transformOrigin: 'left', duration: reduce ? 0 : 0.5, ease: 'power2.out' });
    },

    /** 검수 화면 "고칠 곳"을 눌렀을 때 해당 블록으로 스크롤 후 노란 강조 */
    flash: function (el, scroller) {
      if (!el) return;
      var go = function () { if (g && !reduce) g.fromTo(el, { backgroundColor: 'var(--hl)' }, { backgroundColor: 'rgba(0,0,0,0)', duration: 1.2, clearProps: 'backgroundColor' }); };
      if (scroller) { el.scrollIntoView({ behavior: reduce ? 'auto' : 'smooth', block: 'start' }); setTimeout(go, reduce ? 0 : 450); }
      else go();
    },

    /** 잘못 입력한 칸 흔들기 */
    shake: function (el) { if (g && !reduce && el) g.fromTo(el, { x: -6 }, { x: 0, duration: 0.5, ease: 'elastic.out(1,0.3)' }); },

    fade: fade,
    reduced: reduce
  };

  global.GYMotion = M;
})(window);
