; unsat: x > y and x / y != 0
(set-logic QF_BV)
(declare-fun y () (_ BitVec 256))
(declare-fun x () (_ BitVec 256))
(assert (bvult x y))
(assert (not (= (bvudiv x y) #x0000000000000000000000000000000000000000000000000000000000000000)))
(check-sat)
(exit)