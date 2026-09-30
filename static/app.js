// подтверждения и защита от двойного нажатия
document.addEventListener('submit', function (e) {
  var f = e.target, msg = f.dataset.confirm;
  if (msg) {
    var amt = f.elements.amount ? f.elements.amount.value : '';
    if (!confirm(msg.replace('{amount}', amt))) { e.preventDefault(); return; }
  }
  var b = e.submitter;
  if (b) setTimeout(function () { b.disabled = true; }, 0);
});

// превью суммы за оценку
(function () {
  var f = document.getElementById('gradeForm');
  if (!f) return;
  var tariff = JSON.parse(f.dataset.tariff), out = document.getElementById('gradePreview');
  function upd() {
    var kind = f.querySelector('input[name=kind]:checked').value;
    var v = tariff[kind][f.elements.grade.value];
    if (v === undefined) { out.textContent = ''; return; }
    var s = Math.abs(v).toLocaleString('ru-RU') + '\u00a0₽';
    out.textContent = (v > 0 ? '+' : v < 0 ? '−' : '') + s;
    out.className = 'preview ' + (v > 0 ? 'p' : v < 0 ? 'n' : '');
  }
  f.addEventListener('change', upd);
  upd();
})();
