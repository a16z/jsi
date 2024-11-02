(set-logic QF_AUFBV)

(declare-fun p_y_uint256 () (_ BitVec 32))
(declare-fun p_x_uint256 () (_ BitVec 32))

(assert (not (= p_y_uint256 (_ bv0 32))))

(assert
 (let ((?x385 (bvurem p_x_uint256 p_y_uint256)))
 (let (($x682 (bvule ?x385 p_y_uint256)))
 (not $x682))))

(check-sat)