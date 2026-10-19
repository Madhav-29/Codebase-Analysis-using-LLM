package com.acme.shop.order.controller;

import java.util.List;
import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping(value = "/orders")
public class OrderController<T extends Number> extends BaseController<T> implements Auditable, Comparable<OrderController<T>> {
    private final OrderService orderService;
    private int a = 1, b;

    public OrderController(OrderService orderService) {
        this.orderService = orderService;
    }

    @GetMapping(path = {"", "/all"})
    @Secured(Roles.READ)
    public ResponseEntity<List<OrderDto>> list(@RequestParam(required = false) String status) {
        if (status == null) {
            return ResponseEntity.ok(orderService.findAll());
        }
        return ResponseEntity.ok(orderService.findByStatus(status));
    }

    @RequestMapping(value = "/{id}", method = {RequestMethod.PUT, RequestMethod.PATCH})
    public <R> R update(@PathVariable("id") final Integer id, String... tags) throws IllegalStateException {
        return null;
    }

    @DeleteMapping("/{id}")
    public void delete(@PathVariable Integer id) {
        orderService.delete(id);
    }

    public OrderService getOrderService() {
        return orderService;
    }

    @Override
    public String toString() {
        return "OrderController";
    }

    static class Page {
        int number;
    }
}
