const http = require("http");
const fs   = require("fs");
const path = require("path");

const PORT = 3001;

http.createServer((req, res) => {
  fs.readFile(path.join(__dirname, "index.html"), (err, data) => {
    if (err) { res.writeHead(404); res.end("index.html not found"); return; }
    res.writeHead(200, { "Content-Type": "text/html; charset=utf-8" });
    res.end(data);
  });
}).listen(PORT, () => {
  console.log("==============================================");
  console.log("  Frontend  =>  http://localhost:3001");
  console.log("  Backend   =>  http://localhost:8001  (run separately)");
  console.log("==============================================");

  
});
