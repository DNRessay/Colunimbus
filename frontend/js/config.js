// Set to the SAM output "ApiUrl" (no trailing slash). Localhost is used when running `wrangler pages dev`.
const PROD_API = "https://REPLACE-ME.lambda-url.eu-west-1.on.aws";
export const API_BASE = ["localhost", "127.0.0.1"].includes(location.hostname) ? "http://localhost:8000" : PROD_API;
