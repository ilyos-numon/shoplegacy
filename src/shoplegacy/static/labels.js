// Shared Korean labels for trade and area codes.
(function () {
  const trade = { restaurant: "식당", cafe: "카페" };
  const area = { residential: "주택가", station: "역세권", office: "오피스", other: "기타" };
  window.slLabels = {
    trade: trade,
    area: area,
    tradeLabel: function (t) {
      return Object.prototype.hasOwnProperty.call(trade, t) ? trade[t] : t;
    },
  };
})();
