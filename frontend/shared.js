var __create = Object.create;
var __defProp = Object.defineProperty;
var __getOwnPropDesc = Object.getOwnPropertyDescriptor;
var __getOwnPropNames = Object.getOwnPropertyNames;
var __getProtoOf = Object.getPrototypeOf;
var __hasOwnProp = Object.prototype.hasOwnProperty;
var __commonJS = (cb, mod) => function __require() {
  return mod || (0, cb[__getOwnPropNames(cb)[0]])((mod = { exports: {} }).exports, mod), mod.exports;
};
var __copyProps = (to, from, except, desc) => {
  if (from && typeof from === "object" || typeof from === "function") {
    for (let key of __getOwnPropNames(from))
      if (!__hasOwnProp.call(to, key) && key !== except)
        __defProp(to, key, { get: () => from[key], enumerable: !(desc = __getOwnPropDesc(from, key)) || desc.enumerable });
  }
  return to;
};
var __toESM = (mod, isNodeMode, target) => (target = mod != null ? __create(__getProtoOf(mod)) : {}, __copyProps(
  isNodeMode || !mod || !mod.__esModule ? __defProp(target, "default", { value: mod, enumerable: true }) : target,
  mod
));

var require_main = __commonJS({
  "node_modules/ngeohash/main.js"(exports, module) {
    var BASE32_CODES = "0123456789bcdefghjkmnpqrstuvwxyz";
    var BASE32_CODES_DICT = {};
    for (i = 0; i < BASE32_CODES.length; i++) {
      BASE32_CODES_DICT[BASE32_CODES.charAt(i)] = i;
    }
    var i;
    var ENCODE_AUTO = "auto";
    var MIN_LAT = -90;
    var MAX_LAT = 90;
    var MIN_LON = -180;
    var MAX_LON = 180;
    var SIGFIG_HASH_LENGTH = [0, 5, 7, 8, 11, 12, 13, 15, 16, 17, 18];
    var encode = function(latitude, longitude, numberOfChars) {
      if (numberOfChars === ENCODE_AUTO) {
        if (typeof latitude === "number" || typeof longitude === "number") {
          throw new Error("string notation required for auto precision.");
        }
        var decSigFigsLat = latitude.split(".")[1].length;
        var decSigFigsLong = longitude.split(".")[1].length;
        var numberOfSigFigs = Math.max(decSigFigsLat, decSigFigsLong);
        numberOfChars = SIGFIG_HASH_LENGTH[numberOfSigFigs];
      } else if (numberOfChars === void 0) {
        numberOfChars = 9;
      }
      var chars = [], bits = 0, bitsTotal = 0, hash_value = 0, maxLat = MAX_LAT, minLat = MIN_LAT, maxLon = MAX_LON, minLon = MIN_LON, mid;
      while (chars.length < numberOfChars) {
        if (bitsTotal % 2 === 0) {
          mid = (maxLon + minLon) / 2;
          if (longitude > mid) {
            hash_value = (hash_value << 1) + 1;
            minLon = mid;
          } else {
            hash_value = (hash_value << 1) + 0;
            maxLon = mid;
          }
        } else {
          mid = (maxLat + minLat) / 2;
          if (latitude > mid) {
            hash_value = (hash_value << 1) + 1;
            minLat = mid;
          } else {
            hash_value = (hash_value << 1) + 0;
            maxLat = mid;
          }
        }
        bits++;
        bitsTotal++;
        if (bits === 5) {
          var code = BASE32_CODES[hash_value];
          chars.push(code);
          bits = 0;
          hash_value = 0;
        }
      }
      return chars.join("");
    };
    var decode_bbox = function(hash_string) {
      var isLon = true, maxLat = MAX_LAT, minLat = MIN_LAT, maxLon = MAX_LON, minLon = MIN_LON, mid;
      var hashValue = 0;
      for (var i2 = 0, l = hash_string.length; i2 < l; i2++) {
        var code = hash_string[i2].toLowerCase();
        hashValue = BASE32_CODES_DICT[code];
        for (var bits = 4; bits >= 0; bits--) {
          var bit = hashValue >> bits & 1;
          if (isLon) {
            mid = (maxLon + minLon) / 2;
            if (bit === 1) minLon = mid; else maxLon = mid;
          } else {
            mid = (maxLat + minLat) / 2;
            if (bit === 1) minLat = mid; else maxLat = mid;
          }
          isLon = !isLon;
        }
      }
      return [minLat, minLon, maxLat, maxLon];
    };
    var decode = function(hashString) {
      var bbox = decode_bbox(hashString);
      var lat = (bbox[0] + bbox[2]) / 2;
      var lon = (bbox[1] + bbox[3]) / 2;
      var latErr = bbox[2] - lat;
      var lonErr = bbox[3] - lon;
      return { latitude: lat, longitude: lon, error: { latitude: latErr, longitude: lonErr } };
    };
    var geohash = { "ENCODE_AUTO": ENCODE_AUTO, "encode": encode, "decode": decode, "decode_bbox": decode_bbox };
    module.exports = geohash;
  }
});

var import_ngeohash = __toESM(require_main());

function posFromHash(hash) {
  const { latitude: lat, longitude: lon } = import_ngeohash.default.decode(hash);
  return [lat, lon];
}

function haversineMiles(a, b) {
  const R = 3958.8;
  const toRad = (deg) => deg * Math.PI / 180;
  const [lat1, lon1] = a;
  const [lat2, lon2] = b;
  const dLat = toRad(lat2 - lat1);
  const dLon = toRad(lon2 - lon1);
  const h = Math.sin(dLat / 2) ** 2 + Math.cos(toRad(lat1)) * Math.cos(toRad(lat2)) * Math.sin(dLon / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(h));
}

var centerPos = [37.3382, -121.8863];
var maxDistanceMiles = 0;
var initialZoom = 10;

let configPromise = null;
async function loadConfig() {
  if (configPromise) return configPromise;
  configPromise = fetch('/config')
    .then(res => res.json())
    .then(config => {
      centerPos = config.centerPos || centerPos;
      maxDistanceMiles = config.maxDistanceMiles || maxDistanceMiles;
      initialZoom = config.initialZoom || initialZoom;
      return config;
    })
    .catch(err => {
      console.warn('Failed to load config from server, using defaults:', err);
      return { centerPos, maxDistanceMiles, initialZoom };
    });
  return configPromise;
}

function ageInDays(time) {
  const dayInMillis = 24 * 60 * 60 * 1000;
  return (Date.now() - new Date(time)) / dayInMillis;
}

function pushMap(map, key, value) {
  const items = map.get(key);
  if (items) items.push(value);
  else map.set(key, [value]);
}

function sigmoid(value, scale = 0.25, center = 0) {
  const g = scale * (value - center);
  return 1 / (1 + Math.exp(-g));
}

var TIME_TRUNCATION = 1e5;
function fromTruncatedTime(truncatedTime) {
  return truncatedTime * TIME_TRUNCATION;
}

var export_geo = import_ngeohash.default;
export {
  ageInDays,
  centerPos,
  fromTruncatedTime,
  export_geo as geo,
  haversineMiles,
  initialZoom,
  loadConfig,
  maxDistanceMiles,
  posFromHash,
  pushMap,
  sigmoid,
};
