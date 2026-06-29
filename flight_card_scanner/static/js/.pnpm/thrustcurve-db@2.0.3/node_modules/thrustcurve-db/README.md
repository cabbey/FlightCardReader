# thrustcurve-db

This module is a rebundling of the model rocket motor data available on John Coker's excellent [thrustcurve.org](https://thrustcurve.org) website ("TC") as a stand-alone JSON file. The data is an array of motor items consistent with TC's [`SearchResponse#results` schema](https://app.swaggerhub.com/apis/JCSW7/thrust-curve_org_api/1.0.3#/SearchResponse).

See also, the included [TypeScript definitions](https://github.com/broofa/thrustcurve-db/blob/main/thrustcurve.d.ts).

### Alterations

In addition to the `SearchResponse` data, the following alterations have been made:

- All `number`s are rounded to a precision of 4 digits.
- Most (but not all) motors include a `samples` array containing the thrust data found in the TC `/api/vi/download` endpoint.
- `samples` data is normalized to insure the first data point is always `[0, 0]`

For full details of how this data set is compiled, please refer to the [`build/build.js`](https://github.com/broofa/thrustcurve-db/blob/main/build/build.js) script in this repository.

## Installation

```
npm i thrustcurve-db
yarn add thrustcurve-db
```

## Usage

### ESM

```js
import MOTORS from 'thrustcurve-db';
```

### CommonJS

```js
const MOTORS = require('thrustcurve-db');
```

Note: Users running `node` may need to supply the [`--experimental-json-modules`](https://nodejs.org/docs/latest-v12.x/api/all.html#esm_experimental_json_modules) flag

### Fetch from JSDelivr CDN:

```js
const MOTORS = await fetch(
  'https://cdn.jsdelivr.net/npm/thrustcurve-db@latest/thrustcurve-db.json'
).then(res => res.json());
```

## Example

```js
// Find all J motors currently in production
MOTORS.filter(m => m.availability === 'regular' && m.impulseClass === 'J');
```

## Issues & Contributions

The data provided here is sourced from thrustcurve.org. Omissions and errors in rocket data should be directed there. Any systematic problems or suggestions for how the data here is presented may be [reported
here](https://github.com/broofa/thrustcurve-db/issues).
