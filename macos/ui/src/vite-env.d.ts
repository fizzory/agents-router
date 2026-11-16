// Vite's ?raw imports: the file's text (tests read motion.css tokens this way).
declare module '*?raw' {
  const text: string;
  export default text;
}
