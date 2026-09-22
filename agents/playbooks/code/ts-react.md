## TypeScript + React

Conventions for frontend work in `better-n8n-frontend/`.

- `npm run build` runs the TypeScript check plus the Vite bundle; `npm run lint`
  runs ESLint.
- Colocate tests as `src/<dir>/__tests__/<name>.test.ts`.
- Keep ticking clocks in leaf components so worker updates do not re-render the
  transcript; `memo` anything that renders often.
- Never render raw tool arguments verbatim — describe the call in a sentence.
