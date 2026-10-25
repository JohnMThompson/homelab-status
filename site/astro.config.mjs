import { defineConfig } from 'astro/config';

export default defineConfig({
  site: 'https://status.johnthompson.io',
  output: 'static',
  build: { format: 'directory' }
});
