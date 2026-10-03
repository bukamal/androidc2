/* 3D globe with device markers. */

const GlobeViz = (() => {
  let globe = null;
  let container = null;

  function init(containerId) {
    container = document.getElementById(containerId);
    if (!container || typeof Globe === "undefined") return;

    globe = Globe()(container)
      .backgroundColor("rgba(0,0,0,0)")
      .showAtmosphere(true)
      .atmosphereColor("#00e0ff")
      .atmosphereAltitude(0.22)
      .globeImageUrl("//unpkg.com/three-globe/example/img/earth-dark.jpg")
      .bumpImageUrl("//unpkg.com/three-globe/example/img/earth-topology.png")
      .pointOfView({ lat: 25, lng: 20, altitude: 2.6 }, 0);

    globe.pointsData([])
      .pointLat("lat")
      .pointLng("lng")
      .pointColor(d => d.online ? "#00e0ff" : "#6472a0")
      .pointAltitude(0.01)
      .pointRadius(0.4)
      .pointsMerge(false)
      .pointLabel(d => `
        <div style="font-family:monospace;font-size:12px;color:#d8e2ff">
          <b>${d.name}</b><br>
          ${d.ip || ""}
        </div>
      `);

    globe.arcsData([])
      .arcStartLat("startLat")
      .arcStartLng("startLng")
      .arcEndLat("endLat")
      .arcEndLng("endLng")
      .arcColor(() => ["#00e0ff", "#7b5cff"])
      .arcDashLength(0.4)
      .arcDashGap(0.15)
      .arcDashAnimateTime(2200)
      .arcStroke(0.3);

    const controls = globe.controls();
    controls.autoRotate = true;
    controls.autoRotateSpeed = 0.35;
    controls.enableZoom = true;
    controls.enablePan = false;

    const resize = () => {
      if (!container) return;
      globe.width(container.clientWidth);
      globe.height(container.clientHeight);
    };
    window.addEventListener("resize", resize);
    setTimeout(resize, 80);
    setTimeout(resize, 400);
  }

  function setDevices(devices) {
    if (!globe) return;
    const valid = devices.filter(d => d.latitude && d.longitude);
    const pts = valid.map(d => ({
      lat: d.latitude,
      lng: d.longitude,
      name: d.model || "device",
      ip: d.ip_address,
      id: d.id,
      online: d.is_online,
    }));
    globe.pointsData(pts);

    const arcs = pts.map(p => ({
      startLat: 0,
      startLng: 0,
      endLat: p.lat,
      endLng: p.lng,
      color: ["#00e0ff", "#7b5cff"],
    }));
    globe.arcsData(arcs);
  }

  function focusOn(lat, lng) {
    if (!globe) return;
    globe.pointOfView({ lat, lng, altitude: 1.8 }, 1200);
  }

  return { init, setDevices, focusOn };
})();

window.GlobeViz = GlobeViz;
