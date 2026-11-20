// 作品后端的替身：一个最小的 Node HTTP 服务，只在本机回环上听。
// 每次回话都读一遍作品盘上的文件（参数给路径），回它的内容——回对了才说明作品的 /data 真挂上了、读得到。
const fs = require('fs');
const file = process.argv[2] || '/work/.berth-marker';
require('http').createServer((q, s) => {
  fs.readFile(file, (e, b) => { if (e) { s.statusCode = 500; s.end('no-data'); } else { s.end(b); } });
}).listen(8080, '127.0.0.1');
