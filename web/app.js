"use strict";
// CASCADE dam dossier page. It reads only what the pipeline publishes:
//   reports/<dam>/dossier.json          routes to the dam, lake observations
//   obs/<lake>/<date>/<scene>.geojson   each observed lake outline
//   terrain/<lake>/channel.json         the traced flood path and dam point
// plus img/images.json and the true-colour images shipped with the page.

const TIME_ZONE = "Asia/Kolkata";
const OLYMPIC_POOL_M3 = 2500;
const RELIEF_TILES =
  "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png";
const HIDDEN_CLASSES = { snow_ice: "snow and ice", cloud: "cloud", shadow: "shadow" };

const query = new URLSearchParams(location.search).get("dam") || "";
const damId = /^[a-z0-9_]+$/.test(query) ? query : "teesta_iii";

async function getJson(path) {
  const response = await fetch(path, { cache: "no-store" });
  if (!response.ok) throw new Error(`${path}: HTTP ${response.status}`);
  return response.json();
}

function el(tag, attributes = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, value);
  node.append(...children);
  return node;
}

const clockFormat = new Intl.DateTimeFormat("en-IN", {
  timeZone: TIME_ZONE, hour: "2-digit", minute: "2-digit", hour12: false });
const dayFormat = new Intl.DateTimeFormat("en-GB", {
  timeZone: TIME_ZONE, day: "numeric", month: "long", year: "numeric" });
const clock = (iso) => clockFormat.format(new Date(iso));
const day = (iso) => dayFormat.format(new Date(iso));
const number = (value) => Math.round(value).toLocaleString("en-IN");

function duration(minutes) {
  const total = Math.round(minutes);
  const hours = Math.floor(total / 60);
  return hours ? `${hours} h ${total % 60} min` : `${total} min`;
}

function lakeName(lakeId) {
  return lakeId.split("_").map((w) => w[0].toUpperCase() + w.slice(1)).join(" ") + " Lake";
}

function areaText(obs) {
  const low = obs.area_low_km2, high = obs.area_high_km2;
  if (obs.status === "best") return `${low.toFixed(2)} km²`;
  if (obs.status === "range") return `${low.toFixed(2)}–${high.toFixed(2)} km²`;
  if (obs.status === "frozen") return "Frozen over";
  if (obs.status === "rejected") return "Too hidden to measure";
  return "No usable image";
}

function statusText(obs, outline) {
  if (obs.status === "best") return "Clear view of the whole lake.";
  if (obs.status !== "range" || !outline) return "";
  const props = outline.properties;
  const [cause] = Object.keys(HIDDEN_CLASSES)
    .sort((a, b) => (props.class_shares[b] || 0) - (props.class_shares[a] || 0));
  const share = Math.round(props.obscured_fraction * 100);
  return `Partly hidden by ${HIDDEN_CLASSES[cause]} (${share}% of the lake's full extent), so the area is a range.`;
}

function relation(obs, route) {
  if (!route) return "";
  return new Date(obs.acquired) < new Date(route.release_time)
    ? "before the burst" : "after the burst";
}

function fact(label, value, note) {
  return el("div", { class: "fact" }, el("dt", {}, label),
    el("dd", {}, value, el("span", { class: "note" }, note)));
}

function renderAnswer(dossier, route) {
  document.getElementById("dam-name").textContent = dossier.dam_name;
  const minutesToPeak = (new Date(route.peak_time) - new Date(route.arrival_time)) / 60000;
  document.getElementById("headline").textContent =
    `Water from ${lakeName(route.lake_id)} reaches the dam ` +
    `${duration(route.warning_minutes)} after the lake bursts.`;
  document.getElementById("facts").replaceChildren(
    fact("Time to act", duration(route.warning_minutes),
      `burst at ${clock(route.release_time)}, water at the dam at ${clock(route.arrival_time)} IST`),
    fact("Peak flow", `${number(route.peak_m3s)} m³/s`,
      `about ${Math.round(route.peak_m3s / OLYMPIC_POOL_M3)} Olympic pools every second, ` +
      `${Math.round(minutesToPeak)} min after the water arrives`),
    fact("Water depth at the dam", `${route.depth_m} m`, "at the peak"),
    fact("Distance down the valley", `${route.channel_km} km`, "from the lake to the dam"));
  document.getElementById("timeline").textContent =
    `In the 2023 replay the lake released about ${number(route.volume_m3 / 1e6)} million m³ ` +
    `at ${clock(route.release_time)} IST on ${day(route.release_time)}. ` +
    `Water reached the dam at ${clock(route.arrival_time)} IST and peaked at ` +
    `${clock(route.peak_time)} IST.`;
  document.getElementById("generated").textContent =
    `Dossier generated ${day(dossier.generated)}, ${clock(dossier.generated)} IST, ` +
    "by the latest pipeline run.";
}

function renderObservations(observations, outlines, route) {
  document.getElementById("observations").replaceChildren(...observations.map((obs) =>
    el("li", {},
      el("span", {}, `${day(obs.acquired)}, ${relation(obs, route)}`),
      el("span", { class: "area" }, areaText(obs)),
      el("span", { class: "status" }, statusText(obs, outlines[obs.scene_id])))));
}

function renderImages(images, observations, route) {
  const byScene = Object.fromEntries(observations.map((obs) => [obs.scene_id, obs]));
  const figures = images.filter((image) => byScene[image.scene_id]).map((image) => {
    const obs = byScene[image.scene_id];
    const when = `${day(obs.acquired)}, ${relation(obs, route)}`;
    return el("figure", {},
      el("img", { src: image.file, width: "696", height: "562", loading: "lazy",
        alt: `True-colour satellite image of ${lakeName(obs.lake_id)} on ${when}` }),
      el("figcaption", {}, `${when}: ${areaText(obs)}. Sentinel-2 true colour.`));
  });
  document.getElementById("images").replaceChildren(...figures);
}

function bounds(points) {
  const lons = points.map((p) => p[0]), lats = points.map((p) => p[1]);
  return [[Math.min(...lons), Math.min(...lats)], [Math.max(...lons), Math.max(...lats)]];
}

function outlinePoints(feature) {
  return feature.geometry.coordinates.flat(feature.geometry.type === "MultiPolygon" ? 2 : 1);
}

function label(text, kind) {
  return el("div", { class: `map-label ${kind}` }, text);
}

function renderMap(channel, observations, outlines, images, route) {
  const path = channel.nodes_lonlat;
  const valley = bounds(path);
  const imageByScene = Object.fromEntries(images.map((image) => [image.scene_id, image]));
  const shown = observations.filter((obs) => outlines[obs.scene_id]);
  const map = new maplibregl.Map({
    container: "map",
    bounds: valley,
    fitBoundsOptions: { padding: 40 },
    cooperativeGestures: true,
    attributionControl: { compact: true },
    style: {
      version: 8,
      sources: {
        relief: { type: "raster-dem", tiles: [RELIEF_TILES], tileSize: 256,
          encoding: "terrarium", maxzoom: 14,
          attribution: "Relief: Terrain Tiles (Mapzen, Registry of Open Data on AWS)" },
      },
      layers: [
        { id: "paper", type: "background", paint: { "background-color": "#ece8df" } },
        { id: "relief", type: "hillshade", source: "relief",
          paint: { "hillshade-exaggeration": 0.55, "hillshade-shadow-color": "#5b5245" } },
      ],
    },
  });
  map.addControl(new maplibregl.NavigationControl({ showCompass: false }));

  map.on("load", () => {
    map.addSource("path", { type: "geojson",
      data: { type: "Feature", geometry: { type: "LineString", coordinates: path } } });
    map.addLayer({ id: "path-casing", type: "line", source: "path",
      paint: { "line-color": "#ffffff", "line-width": 7 } });
    map.addLayer({ id: "path", type: "line", source: "path",
      paint: { "line-color": "#c2410c", "line-width": 3.5 } });
    map.addSource("lake", { type: "geojson", data: outlines[shown[0].scene_id] });
    map.addLayer({ id: "lake-fill", type: "fill", source: "lake",
      paint: { "fill-color": "#1d6fb8", "fill-opacity": 0.25 } });
    map.addLayer({ id: "lake-line", type: "line", source: "lake",
      paint: { "line-color": "#1d6fb8", "line-width": 2 } });

    const lakeBox = bounds(outlinePoints(outlines[shown[0].scene_id]));
    new maplibregl.Marker({ element: label(lakeName(route.lake_id), "lake"), anchor: "bottom" })
      .setLngLat([(lakeBox[0][0] + lakeBox[1][0]) / 2, lakeBox[1][1]]).addTo(map);
    new maplibregl.Marker({ color: "#c2410c" }).setLngLat(channel.meta.dam_lonlat).addTo(map);
    new maplibregl.Marker({ element: label("Teesta-III dam", "dam"), anchor: "left", offset: [14, 0] })
      .setLngLat(channel.meta.dam_lonlat).addTo(map);

    const buttons = [];
    function press(active) {
      for (const button of buttons) button.setAttribute("aria-pressed", String(button === active));
    }
    function showLake(obs, button) {
      map.getSource("lake").setData(outlines[obs.scene_id]);
      const image = imageByScene[obs.scene_id];
      if (image) {
        const source = map.getSource("lake-image");
        if (source) {
          source.updateImage({ url: image.file, coordinates: image.corners_lonlat });
        } else {
          map.addSource("lake-image", { type: "image", url: image.file,
            coordinates: image.corners_lonlat });
          map.addLayer({ id: "lake-image", type: "raster", source: "lake-image" }, "lake-fill");
        }
      }
      map.fitBounds(image ? bounds(image.corners_lonlat) : lakeBox, { padding: 24 });
      press(button);
    }
    for (const obs of shown) {
      const button = el("button", { type: "button", "aria-pressed": "false" },
        `${day(obs.acquired)} (${relation(obs, route)})`);
      button.addEventListener("click", () => showLake(obs, button));
      buttons.push(button);
    }
    const whole = el("button", { type: "button", "aria-pressed": "true" }, "Whole valley");
    whole.addEventListener("click", () => {
      map.fitBounds(valley, { padding: 40 });
      press(whole);
    });
    buttons.push(whole);
    document.getElementById("date-switch").replaceChildren(...buttons);
  });
}

async function main() {
  document.getElementById("print-button").addEventListener("click", () => window.print());
  let dossier;
  try {
    dossier = await getJson(`reports/${damId}/dossier.json`);
  } catch (error) {
    const headline = document.getElementById("headline");
    headline.textContent = `The dossier could not be loaded (${error.message}).`;
    headline.classList.add("error");
    return;
  }
  const route = dossier.lakes[0];
  const observations = [...dossier.observations]
    .sort((a, b) => a.acquired.localeCompare(b.acquired));
  renderAnswer(dossier, route);

  const [outlineList, channel, images] = await Promise.all([
    Promise.all(observations.map((obs) => getJson(obs.outline_key).catch(() => null))),
    getJson(`terrain/${route.lake_id}/channel.json`),
    getJson("img/images.json").catch(() => []),
  ]);
  const outlines = {};
  observations.forEach((obs, i) => { if (outlineList[i]) outlines[obs.scene_id] = outlineList[i]; });
  renderObservations(observations, outlines, route);
  renderImages(images, observations, route);
  try {
    renderMap(channel, observations, outlines, images, route);
  } catch (error) {
    document.getElementById("map").textContent = `The map could not be drawn (${error.message}).`;
  }
}

main();
