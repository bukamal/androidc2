/* 3D globe with device points and arcs. */

const GlobeViz = (() => {
  let globe;

  function init(containerId) {
    globe = Globe()(document.getElementById(containerId))
      .backgroundColor("rgba(0,0,0,0)")
      .showAtmosphere(true)
      .atmosphereColor("#00e0ff")
      .atmosphereAltitude(0.22)
      .globeImageUrl("//unpkg.com/three-globe/example/img/earth-dark.jpg")
      .bumpImageUrl("//unpkg.com/three-globe/example/img/earth-topology.png")
      .pointOfView({ lat: 25, lng: 20, altitude: 2.5 }, 0);

    globe.pointsData([])
      .pointLat("lat").pointLng("lng")
      .pointColor(() => "#00e0ff")
      .pointAltitude(0.01)
      .pointRadius(0.35)
      .pointsMerge(false)
      .pointLabel(d => `${d.name}<br>${d.ip || ""}`);

    globe.arcsData([])
      .arcStartLat("startLat").arcStartLng("startLng")
      .arcEndLat("endLat").arcEndLng("endLng")
      .arcColor(() => ["#00e0ff", "#7b5cff"])
      .arcDashLength(0.4)
      .arcDashGap(0.15)
      .arcDashAnimateTime(2000)
      .arcStroke(0.35);

    const controls = globe.controls();
    controls.autoRotate = true;
    controls.autoRotateSpeed = 0.4;
    controls.enableZoom = true;

    window.addEventListener("resize", () => {
      globe.width(window.innerWidth - 320);
      globe.height(window.innerHeight);
    });
  }

  function setDevices(devices) {
    const valid = devices.filter(d => d.latitude && d.longitude);
    const pts = valid.map(d => ({
      lat: d.latitude,
      lng: d.longitude,
      name: d.model || "device",
      ip: d.ip_address,
      id: d.id,
      online: d.is_online
    }));
    globe.pointsData(pts);

    const arcsArr = pts.map(p => ({
      startLat: 0, startLng: 0,
      endLat: p.lat, endLng: p.lng,
      color: ["#00e0ff", "#7b5cff"]
    }));
    globe.arcsData(arcsArr);
  }

  function focusOn(lat, lng) {
    globe.pointOfView({ lat, lng, altitude: 1.6 }, 1200);
  }

  return { init, setDevices, focusOn };
})();

window.GlobeViz = GlobeViz;
