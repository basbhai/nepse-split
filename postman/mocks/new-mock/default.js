/**
 * Get started with your virtual shopping cart.
 *   1. POST /cart/items   add items to buy
 *   2. GET  /cart         see what's in the cart
 *   3. POST /checkout     place the order
 * 
 * Click Start to build your cart and checkout.
 * 
 * Add more routes and annotate using @endpoint <method> /<path>.
 * You can also select existing requests and examples to mock.
 * 
 * Deploying this mock gives you a url which can be shared, used in CI and more.
 */
const http = require("http");
const PORT = process.env.PORT || 4500;
const JSON_HEADER = { "Content-Type": "application/json" };

const server = http.createServer(async (req, res) => {
  const { method, url } = req;
  const cart = (await pm.state.get("cart")) || [];

  // @endpoint POST /cart/items
  if (method === "POST" && url === "/cart/items") {
    const b = await new Promise((resolve) => {
      let raw = "";
      req.on("data", (c) => (raw += c));
      req.on("end", () => { try { resolve(raw ? JSON.parse(raw) : {}); } catch { resolve({}); } });
    });
    cart.push({ name: b.name || "Iphone 18", price: b.price ?? 999, qty: b.qty ?? 1 });
    await pm.state.set("cart", cart);
    res.writeHead(201, JSON_HEADER);
    return res.end(JSON.stringify({ items: cart }));
  }

  // @endpoint GET /cart
  if (method === "GET" && url === "/cart") {
    res.writeHead(200, JSON_HEADER);
    return res.end(JSON.stringify({ items: cart }));
  }

  // @endpoint POST /checkout
  if (method === "POST" && url === "/checkout") {
    if (!cart.length) {
      res.writeHead(400, JSON_HEADER);
      return res.end(JSON.stringify({ error: "Cart is empty." }));
    }
    await pm.state.set("cart", []);
    res.writeHead(201, JSON_HEADER);
    return res.end(JSON.stringify({ orderId: `order-${Date.now()}`, items: cart, status: "confirmed" }));
  }

  // Select requests and examples to mock
  if (pm.mock.matchRequest("SELECT REQUEST", req)) {
    return pm.mock.sendExample("SELECT EXAMPLE", res);
  }

  res.writeHead(404, JSON_HEADER);
  res.end(JSON.stringify({
    error: "Endpoint not defined",
    message: `Please add ${url} endpoint to this mock.`
  }));
});

server.listen(PORT, () => console.log(`Mock server on port ${PORT}`));