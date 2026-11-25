// Team hours worked vs expected for the last 14 days (data from /api/v1/trend).
(async function () {
  const canvas = document.getElementById("trend");
  if (!canvas || typeof Chart === "undefined") return;
  const res = await fetch("/api/v1/trend?days=14", { credentials: "same-origin" });
  if (!res.ok) return;
  const { days } = await res.json();
  const label = (d) => new Date(d + "T00:00:00").toLocaleDateString(undefined, { weekday: "short", day: "numeric" });
  new Chart(canvas, {
    data: {
      labels: days.map((d) => label(d.day)),
      datasets: [
        { type: "bar", label: "Worked (h)", data: days.map((d) => d.worked_h), backgroundColor: "#4f46e5", borderRadius: 6 },
        { type: "line", label: "Expected (h)", data: days.map((d) => d.expected_h), borderColor: "#d97706",
          backgroundColor: "#d97706", borderDash: [6, 4], pointRadius: 3, tension: 0.2 },
      ],
    },
    options: {
      responsive: true,
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: { position: "bottom" },
        tooltip: { callbacks: { afterBody: (items) => `Absent: ${days[items[0].dataIndex].absent}` } },
      },
      scales: { y: { beginAtZero: true, title: { display: true, text: "hours" } } },
    },
  });
})();
