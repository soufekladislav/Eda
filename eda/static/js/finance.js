(function () {
  const canvas = document.getElementById('edaFinanceChart');
  const payload = document.getElementById('edaFinanceData');
  if (!canvas || !payload) return;
  let rows = [];
  try { rows = JSON.parse(payload.textContent || '[]'); } catch (_) { return; }
  if (!Array.isArray(rows)) return;
  if (!rows.length) {
    const note = document.createElement('p'); note.className = 'muted-text';
    note.textContent = 'Zatím nemáme evidované pohyby, ze kterých lze sestavit graf.';
    canvas.replaceWith(note); return;
  }
  function render() {
    if (!window.Chart) return;
    new window.Chart(canvas, {
      type:'bar', data:{labels:rows.map(x=>x.month),datasets:[
        {label:'Příjmy',data:rows.map(x=>x.income),backgroundColor:'#9EC415'},
        {label:'Náklady',data:rows.map(x=>x.expense),backgroundColor:'#E40058'}]},
      options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{position:'bottom'}},
        scales:{y:{beginAtZero:true,ticks:{callback:value=>Number(value).toLocaleString('cs-CZ')+' Kč'}}}}
    });
  }
  if (window.Chart) render();
  else window.addEventListener('load', render, {once:true});
})();
