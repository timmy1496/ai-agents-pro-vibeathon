import http from 'k6/http';

// Рівний фон трафіку на chaos-svc: 5 rps без обмежень, щоб golden signals були живі.
export const options = {
  scenarios: {
    steady: {
      executor: 'constant-arrival-rate',
      rate: 5,
      timeUnit: '1s',
      duration: '720h',
      preAllocatedVUs: 10,
      maxVUs: 50,
    },
  },
  thresholds: {},          // 5xx під час chaos — очікувані, не валимо прогін
  discardResponseBodies: true,
};

export default function () {
  http.get('http://chaos-svc:8080/', { timeout: '5s' });
}
