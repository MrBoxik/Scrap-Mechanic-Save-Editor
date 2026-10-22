(module
  (memory (export "memory") 16)
  (func (export "swap_rb") (param $count i32)
    (local $index i32)
    (local $value i32)
    (block $done
      (loop $next
        local.get $index
        local.get $count
        i32.ge_u
        br_if $done

        local.get $index
        i32.const 4
        i32.mul
        i32.load
        local.set $value

        local.get $index
        i32.const 4
        i32.mul
        local.get $value
        i32.const 0xff00ff00
        i32.and
        local.get $value
        i32.const 0xff
        i32.and
        i32.const 16
        i32.shl
        i32.or
        local.get $value
        i32.const 16
        i32.shr_u
        i32.const 0xff
        i32.and
        i32.or
        i32.store

        local.get $index
        i32.const 1
        i32.add
        local.set $index
        br $next
      )
    )
  )
)
